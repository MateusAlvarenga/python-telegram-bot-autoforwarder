import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from bot.config import BotConfig
from bot.db import normalize_database_url
from bot.handlers import create_routine, parse_cursor_spec, parse_routine_spec
from bot.main import build_application
from bot.routines import RoutineScheduler, message_belongs_to_topic


class ConfigurationTests(unittest.TestCase):
    def test_normalizes_railway_postgres_url(self) -> None:
        self.assertEqual(
            normalize_database_url("postgres://user:pass@host:5432/db"),
            "postgresql://user:pass@host:5432/db",
        )

    def test_builds_private_control_commands(self) -> None:
        application = build_application(
            BotConfig(
                bot_token="123456:token",
                owner_id=123,
                database_url="postgresql://user:pass@host/db",
                api_id=1,
                api_hash="hash",
                telethon_session="session",
                copier_bot_token=None,
                log_level=20,
            )
        )

        self.assertEqual(len(application.handlers[0]), 13)


class RoutineSpecTests(unittest.TestCase):
    def test_parses_chat_ids_optional_topics_and_interval(self) -> None:
        self.assertEqual(
            parse_routine_spec("updates|-100100|-|-100200|456|60"),
            ("updates", "-100100", None, "-100200", 456, 60, None),
        )

    def test_rejects_missing_fields_and_non_positive_values(self) -> None:
        for value in (
            "updates|-100100|-|-100200|-",
            "updates|-100100|0|-100200|-|60",
            "updates|-100100|-|-100200|-|0",
            "updates|-100100|-|-100200|-|60|0",
            "updates|-100100|-|-100200|-|60|many",
            "updates|-100100|-|-100200|-|60|9223372036854775808",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_routine_spec(value)

    def test_parses_optional_positive_post_limit(self) -> None:
        self.assertEqual(
            parse_routine_spec("updates|-100100|-|-100200|-|60|25"),
            ("updates", "-100100", None, "-100200", None, 60, 25),
        )
        self.assertIsNone(parse_routine_spec("updates|-100100|-|-100200|-|60|-")[-1])


class CursorSpecTests(unittest.TestCase):
    def test_parses_routine_id_and_non_negative_cursor(self) -> None:
        self.assertEqual(parse_cursor_spec("7|0"), (7, 0))
        self.assertEqual(parse_cursor_spec("7 | 12345"), (7, 12345))

    def test_rejects_invalid_routine_ids_and_cursors(self) -> None:
        for value in ("0|1", "1|-1", "1|9223372036854775808", "one|2", "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_cursor_spec(value)


class RoutineCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_saves_latest_source_message_as_initial_cursor(self) -> None:
        database = SimpleNamespace(
            create_routine=AsyncMock(return_value={"id": 7, "max_posts_per_run": None})
        )
        scheduler = SimpleNamespace(latest_source_message_id=AsyncMock(return_value=123))
        config = BotConfig(
            bot_token="123456:token",
            owner_id=123,
            database_url="postgresql://user:pass@host/db",
            api_id=1,
            api_hash="hash",
            telethon_session="session",
            copier_bot_token=None,
            log_level=20,
        )
        context = SimpleNamespace(
            args=["updates|-100100|25|-100200|-|60"],
            application=SimpleNamespace(
                bot_data={"config": config, "database": database, "scheduler": scheduler}
            ),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
        )

        await create_routine(update, context)

        scheduler.latest_source_message_id.assert_awaited_once_with("-100100", 25)
        database.create_routine.assert_awaited_once_with(
            "updates",
            "-100100",
            25,
            "-100200",
            None,
            60,
            last_message_id=123,
            max_posts_per_run=None,
        )


class TopicFilterTests(unittest.TestCase):
    def test_matches_topic_root_and_nested_replies(self) -> None:
        root = SimpleNamespace(id=25, reply_to=None)
        direct_reply = SimpleNamespace(
            id=30,
            reply_to=SimpleNamespace(reply_to_msg_id=25, reply_to_top_id=None),
        )
        nested_reply = SimpleNamespace(
            id=31,
            reply_to=SimpleNamespace(reply_to_msg_id=30, reply_to_top_id=25),
        )

        self.assertTrue(message_belongs_to_topic(root, 25))
        self.assertTrue(message_belongs_to_topic(direct_reply, 25))
        self.assertTrue(message_belongs_to_topic(nested_reply, 25))
        self.assertFalse(message_belongs_to_topic(nested_reply, 30))

    def test_rejects_messages_without_a_matching_topic(self) -> None:
        message = SimpleNamespace(id=42, reply_to=None)

        self.assertFalse(message_belongs_to_topic(message, 25))


class RoutineSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_cursor_uses_latest_message_in_chat_or_topic(self) -> None:
        calls = []

        class SourceClient:
            async def get_entity(self, chat_id):
                return chat_id

            def iter_messages(self, _chat, **kwargs):
                calls.append(kwargs)

                async def messages():
                    if "reply_to" in kwargs:
                        yield SimpleNamespace(id=28)
                    else:
                        yield SimpleNamespace(id=50)

                return messages()

        scheduler = RoutineScheduler(SimpleNamespace(), SourceClient(), SimpleNamespace())

        self.assertEqual(await scheduler.latest_source_message_id("-100100", None), 50)
        self.assertEqual(await scheduler.latest_source_message_id("-100100", 25), 28)
        self.assertEqual(calls, [{"limit": 1}, {"limit": 1, "reply_to": 25}])

    async def test_initial_cursor_uses_topic_root_when_topic_has_no_replies(self) -> None:
        class SourceClient:
            async def get_entity(self, chat_id):
                return chat_id

            def iter_messages(self, _chat, **_kwargs):
                async def messages():
                    if False:
                        yield None

                return messages()

        scheduler = RoutineScheduler(SimpleNamespace(), SourceClient(), SimpleNamespace())

        self.assertEqual(await scheduler.latest_source_message_id("-100100", 25), 25)

    async def test_post_limit_counts_an_album_as_one_and_keeps_it_whole(self) -> None:
        routine = {
            "id": 7,
            "source_chat_id": "-100100",
            "source_topic_id": None,
            "destination_chat_id": "-100200",
            "destination_topic_id": None,
            "last_message_id": 0,
            "max_posts_per_run": 2,
        }
        source_messages = [
            SimpleNamespace(id=1, grouped_id=900, reply_to=None),
            SimpleNamespace(id=2, grouped_id=900, reply_to=None),
            SimpleNamespace(id=3, grouped_id=None, reply_to=None),
            SimpleNamespace(id=4, grouped_id=None, reply_to=None),
        ]
        database = SimpleNamespace(save_cursor=AsyncMock())

        class SourceClient:
            async def get_entity(self, chat_id):
                return chat_id

            def iter_messages(self, _chat, min_id, **_kwargs):
                async def messages():
                    for message in source_messages:
                        if message.id > min_id:
                            yield message

                return messages()

        copier_bot = SimpleNamespace(copy_messages=AsyncMock(return_value=[object(), object(), object()]))
        scheduler = RoutineScheduler(database, SourceClient(), copier_bot)

        copied_count = await scheduler._copy_new_messages(
            routine,
            lease_token=object(),
            stop_event=asyncio.Event(),
        )

        self.assertEqual(copied_count, 3)
        copier_bot.copy_messages.assert_awaited_once()
        self.assertEqual(copier_bot.copy_messages.await_args.kwargs["message_ids"], [1, 2, 3])
        database.save_cursor.assert_awaited_once()
        self.assertEqual(database.save_cursor.await_args.args[2], 3)

    async def test_album_crossing_scan_window_is_deferred_as_one_post(self) -> None:
        source_messages = [
            SimpleNamespace(
                id=message_id,
                grouped_id=900 if message_id >= 110 else None,
                reply_to=(
                    None
                    if message_id == 25
                    else SimpleNamespace(reply_to_top_id=25)
                    if message_id >= 110
                    else None
                ),
            )
            for message_id in range(1, 120)
        ]
        routine = {
            "id": 7,
            "source_chat_id": "-100100",
            "source_topic_id": 25,
            "destination_chat_id": "-100200",
            "destination_topic_id": None,
            "last_message_id": 0,
            "max_posts_per_run": 2,
        }
        database = SimpleNamespace(save_cursor=AsyncMock())

        class SourceClient:
            async def get_entity(self, chat_id):
                return chat_id

            def iter_messages(self, _chat, min_id, limit, **_kwargs):
                async def messages():
                    remaining = [
                        message for message in source_messages if message.id > min_id
                    ][:limit]
                    for message in remaining:
                        yield message

                return messages()

        copier_bot = SimpleNamespace(
            copy_messages=AsyncMock(
                side_effect=[[object()], [object() for _ in range(10)]]
            )
        )
        scheduler = RoutineScheduler(database, SourceClient(), copier_bot)

        await scheduler._copy_new_messages(
            routine,
            lease_token=object(),
            stop_event=asyncio.Event(),
        )

        self.assertEqual(
            [
                call.kwargs["message_ids"]
                for call in copier_bot.copy_messages.await_args_list
            ],
            [[25], list(range(110, 120))],
        )

    async def test_bulk_copy_fallback_keeps_album_messages_together(self) -> None:
        routine = {
            "id": 7,
            "source_chat_id": "-100100",
            "destination_chat_id": "-100200",
            "destination_topic_id": None,
        }
        database = SimpleNamespace(save_cursor=AsyncMock())
        copier_bot = SimpleNamespace(
            copy_messages=AsyncMock(
                side_effect=[
                    BadRequest("Message to copy not found"),
                    [object(), object()],
                ]
            ),
            copy_message=AsyncMock(return_value=object()),
        )
        scheduler = RoutineScheduler(database, SimpleNamespace(), copier_bot)

        copied_count = await scheduler._copy_batch(
            routine,
            [[1, 2], [3]],
            lease_token=object(),
        )

        self.assertEqual(copied_count, 3)
        self.assertEqual(
            [call.kwargs["message_ids"] for call in copier_bot.copy_messages.await_args_list],
            [[1, 2, 3], [1, 2]],
        )
        copier_bot.copy_message.assert_awaited_once_with(
            chat_id=-100200,
            from_chat_id=-100100,
            message_id=3,
            message_thread_id=None,
        )

    async def test_copies_in_batches_checkpoints_and_prevents_overlap(self) -> None:
        routine = {
            "id": 7,
            "name": "updates",
            "source_chat_id": "-100100",
            "source_topic_id": None,
            "destination_chat_id": "-100200",
            "destination_topic_id": None,
            "last_message_id": 0,
            "enabled": False,
            "interval_seconds": 60,
        }
        database = SimpleNamespace(
            claim_routine=AsyncMock(return_value=(routine, 12)),
            save_cursor=AsyncMock(),
            finish_run=AsyncMock(),
        )

        class SourceClient:
            async def get_entity(self, chat_id):
                return chat_id

            def iter_messages(self, *_args, **_kwargs):
                async def messages():
                    yield SimpleNamespace(id=1, reply_to=None)
                    yield SimpleNamespace(id=2, reply_to=None)

                return messages()

        copy_started = asyncio.Event()
        release_copy = asyncio.Event()

        async def hold_copy(**_kwargs):
            copy_started.set()
            await release_copy.wait()
            return [object(), object()]

        copier_bot = SimpleNamespace(copy_messages=AsyncMock(side_effect=hold_copy))
        scheduler = RoutineScheduler(database, SourceClient(), copier_bot)

        self.assertTrue(await scheduler.request_run(routine["id"]))
        await copy_started.wait()
        self.assertFalse(await scheduler.request_run(routine["id"]))
        database.claim_routine.assert_awaited_once()

        release_copy.set()
        await scheduler._tasks[routine["id"]]

        database.save_cursor.assert_awaited_once_with(
            routine["id"],
            database.claim_routine.await_args.args[1],
            2,
            900,
        )
        database.finish_run.assert_awaited_once_with(
            routine["id"],
            12,
            database.claim_routine.await_args.args[1],
            "completed",
            2,
            None,
        )


if __name__ == "__main__":
    unittest.main()
