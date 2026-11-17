from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from telegram import Bot
from telegram.error import BadRequest, RetryAfter
from telethon import TelegramClient

from bot.db import Database

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 900
_BATCH_SIZE = 100
_MAX_ALBUM_SIZE = 10
_MAX_BATCHES_PER_RUN = 10
_RETRY_LIMIT = 5


def message_belongs_to_topic(message: Any, topic_id: int) -> bool:
    if getattr(message, "id", None) == topic_id:
        return True
    reply_to = getattr(message, "reply_to", None)
    top_message_id = getattr(reply_to, "reply_to_top_id", None)
    if top_message_id is not None:
        return top_message_id == topic_id
    return getattr(reply_to, "reply_to_msg_id", None) == topic_id


def _chat_identifier(value: str) -> int | str:
    return int(value) if value.lstrip("-").isdigit() else value


def _is_unavailable_source_message(error: BadRequest) -> bool:
    description = str(error).lower()
    return any(
        phrase in description
        for phrase in (
            "message to copy not found",
            "message can't be copied",
            "message cannot be copied",
            "message_id_invalid",
        )
    )


class RoutineScheduler:
    def __init__(
        self,
        database: Database,
        source_client: TelegramClient,
        copier_bot: Bot,
    ) -> None:
        self._database = database
        self._source_client = source_client
        self._copier_bot = copier_bot
        self._stop_events: dict[int, asyncio.Event] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        if self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(),
                name="routine-scheduler",
            )

    async def stop(self) -> None:
        self._shutdown.set()
        for stop_event in self._stop_events.values():
            stop_event.set()
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=30)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def request_run(self, routine_id: int, manual: bool = True) -> bool:
        if self._shutdown.is_set():
            return False
        local_task = self._tasks.get(routine_id)
        if local_task is not None and not local_task.done():
            return False

        lease_token = uuid4()
        claimed = await self._database.claim_routine(
            routine_id,
            lease_token,
            manual,
            _LEASE_SECONDS,
        )
        if claimed is None:
            return False

        routine, run_id = claimed
        stop_event = asyncio.Event()
        self._stop_events[routine_id] = stop_event
        task = asyncio.create_task(
            self._execute(routine, run_id, lease_token, stop_event),
            name=f"routine-{routine_id}",
        )
        self._tasks[routine_id] = task
        return True

    def request_stop(self, routine_id: int) -> bool:
        stop_event = self._stop_events.get(routine_id)
        if stop_event is None or stop_event.is_set():
            return False
        stop_event.set()
        return True

    def is_running(self, routine_id: int) -> bool:
        task = self._tasks.get(routine_id)
        return task is not None and not task.done()

    async def latest_source_message_id(
        self,
        source_chat_id: str,
        source_topic_id: int | None,
    ) -> int:
        source_chat = await self._source_client.get_entity(_chat_identifier(source_chat_id))
        latest_message_id = source_topic_id or 0
        query = {"limit": 1}
        if source_topic_id is not None:
            query["reply_to"] = source_topic_id

        async for message in self._source_client.iter_messages(source_chat, **query):
            message_id = getattr(message, "id", None)
            if isinstance(message_id, int):
                latest_message_id = max(latest_message_id, message_id)
        return latest_message_id

    async def _scheduler_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                due_routines = await self._database.get_due_routines()
                for routine in due_routines:
                    await self.request_run(routine["id"], manual=False)
            except Exception:
                logger.exception("Could not check for due routines.")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=2)
            except TimeoutError:
                pass

    async def _execute(
        self,
        routine: dict[str, Any],
        run_id: int,
        lease_token: Any,
        stop_event: asyncio.Event,
    ) -> None:
        routine_id = routine["id"]
        copied_count = 0
        status = "completed"
        error_text: str | None = None
        try:
            copied_count = await self._copy_new_messages(routine, lease_token, stop_event)
            if stop_event.is_set():
                status = "stopped"
        except asyncio.CancelledError:
            status = "stopped"
            error_text = "Execution cancelled during service shutdown."
            raise
        except Exception as exc:
            status = "failed"
            error_text = str(exc)[:2000]
            logger.exception("Routine %s failed.", routine_id)
        finally:
            try:
                await asyncio.shield(
                    self._database.finish_run(
                        routine_id,
                        run_id,
                        lease_token,
                        status,
                        copied_count,
                        error_text,
                    )
                )
            except Exception:
                logger.exception("Could not persist the result for routine %s.", routine_id)
            self._stop_events.pop(routine_id, None)
            self._tasks.pop(routine_id, None)
        if status == "failed":
            logger.error("Routine %s will retry at its next scheduled time.", routine_id)

    async def _copy_new_messages(
        self,
        routine: dict[str, Any],
        lease_token: Any,
        stop_event: asyncio.Event,
    ) -> int:
        routine_id = routine["id"]
        source_chat = await self._source_client.get_entity(
            _chat_identifier(routine["source_chat_id"])
        )
        cursor = routine["last_message_id"]
        max_posts_per_run = routine.get("max_posts_per_run")
        posts_processed = 0
        copied_count = 0

        for _ in range(_MAX_BATCHES_PER_RUN):
            if stop_event.is_set():
                break
            if max_posts_per_run is not None and posts_processed >= max_posts_per_run:
                break
            messages = [
                message
                async for message in self._source_client.iter_messages(
                    source_chat,
                    min_id=cursor,
                    reverse=True,
                    limit=_BATCH_SIZE + _MAX_ALBUM_SIZE,
                )
            ]
            if not messages:
                break

            fetched_count = len(messages)
            last_grouped_id = getattr(messages[-1], "grouped_id", None)
            if fetched_count == _BATCH_SIZE + _MAX_ALBUM_SIZE and isinstance(
                last_grouped_id, int
            ):
                first_tail_album_index = next(
                    index
                    for index, message in enumerate(messages)
                    if getattr(message, "grouped_id", None) == last_grouped_id
                )
                messages = messages[:first_tail_album_index]
                if not messages:
                    raise RuntimeError("Could not retrieve a complete Telegram album.")

            latest_scanned_id = max(message.id for message in messages)
            posts_by_key: dict[tuple[str, int], list[int]] = {}
            for message in messages:
                message_id = getattr(message, "id", None)
                if not isinstance(message_id, int):
                    continue
                if (
                    routine["source_topic_id"] is not None
                    and not message_belongs_to_topic(message, routine["source_topic_id"])
                ):
                    continue
                grouped_id = getattr(message, "grouped_id", None)
                post_key = (
                    ("album", grouped_id)
                    if isinstance(grouped_id, int)
                    else ("message", message_id)
                )
                posts_by_key.setdefault(post_key, []).append(message_id)

            posts = list(posts_by_key.values())
            selected_posts: list[list[int]] = []
            selected_message_count = 0
            for post in posts:
                if (
                    max_posts_per_run is not None
                    and posts_processed + len(selected_posts) >= max_posts_per_run
                ):
                    break
                if selected_message_count + len(post) > _BATCH_SIZE:
                    break
                selected_posts.append(post)
                selected_message_count += len(post)

            if posts and not selected_posts:
                raise RuntimeError("A source post exceeds Telegram's 100-message copy limit.")
            if selected_posts:
                copied_count += await self._copy_batch(
                    routine,
                    selected_posts,
                    lease_token,
                )
                posts_processed += len(selected_posts)

            all_posts_selected = len(selected_posts) == len(posts)
            cursor = (
                latest_scanned_id
                if all_posts_selected
                else max(message_id for post in selected_posts for message_id in post)
            )
            await self._database.save_cursor(
                routine_id,
                lease_token,
                cursor,
                _LEASE_SECONDS,
            )
            if max_posts_per_run is not None and posts_processed >= max_posts_per_run:
                break
            if all_posts_selected and fetched_count < _BATCH_SIZE + _MAX_ALBUM_SIZE:
                break

        return copied_count

    async def _copy_batch(
        self,
        routine: dict[str, Any],
        message_posts: list[list[int]],
        lease_token: Any,
    ) -> int:
        source_chat_id = _chat_identifier(routine["source_chat_id"])
        destination_chat_id = _chat_identifier(routine["destination_chat_id"])
        destination_topic_id = routine["destination_topic_id"]
        message_ids = [message_id for post in message_posts for message_id in post]
        try:
            copied_messages = await self._with_rate_limit_retry(
                self._copier_bot.copy_messages,
                chat_id=destination_chat_id,
                from_chat_id=source_chat_id,
                message_ids=message_ids,
                message_thread_id=destination_topic_id,
            )
            return len(copied_messages)
        except BadRequest as exc:
            if not _is_unavailable_source_message(exc):
                raise
            logger.warning(
                "Bulk copy for routine %s was rejected; retrying message by message: %s",
                routine["id"],
                exc,
            )

        copied_count = 0
        for post in message_posts:
            try:
                if len(post) == 1:
                    await self._with_rate_limit_retry(
                        self._copier_bot.copy_message,
                        chat_id=destination_chat_id,
                        from_chat_id=source_chat_id,
                        message_id=post[0],
                        message_thread_id=destination_topic_id,
                    )
                    copied_count += 1
                else:
                    copied_messages = await self._with_rate_limit_retry(
                        self._copier_bot.copy_messages,
                        chat_id=destination_chat_id,
                        from_chat_id=source_chat_id,
                        message_ids=post,
                        message_thread_id=destination_topic_id,
                    )
                    copied_count += len(copied_messages)
            except BadRequest as exc:
                if not _is_unavailable_source_message(exc):
                    raise
                logger.warning(
                    "Skipping unavailable source post %s in routine %s: %s",
                    post,
                    routine["id"],
                    exc,
                )
            await self._database.save_cursor(
                routine["id"],
                lease_token,
                max(post),
                _LEASE_SECONDS,
            )
        return copied_count

    @staticmethod
    async def _with_rate_limit_retry(method: Any, **kwargs: Any) -> Any:
        for attempt in range(_RETRY_LIMIT):
            try:
                return await method(**kwargs)
            except RetryAfter as exc:
                if attempt + 1 == _RETRY_LIMIT:
                    raise
                await asyncio.sleep(float(exc.retry_after) + 1)
        raise RuntimeError("Telegram rate-limit retry loop ended unexpectedly.")
