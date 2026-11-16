import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import BotConfig
from bot.db import normalize_database_url
from bot.handlers import parse_cursor_spec, parse_routine_spec
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
            ("updates", "-100100", None, "-100200", 456, 60),
        )

    def test_rejects_missing_fields_and_non_positive_values(self) -> None:
        for value in (
            "updates|-100100|-|-100200|-",
            "updates|-100100|0|-100200|-|60",
            "updates|-100100|-|-100200|-|0",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_routine_spec(value)


class CursorSpecTests(unittest.TestCase):
    def test_parses_routine_id_and_non_negative_cursor(self) -> None:
        self.assertEqual(parse_cursor_spec("7|0"), (7, 0))
        self.assertEqual(parse_cursor_spec("7 | 12345"), (7, 12345))

    def test_rejects_invalid_routine_ids_and_cursors(self) -> None:
        for value in ("0|1", "1|-1", "1|9223372036854775808", "one|2", "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_cursor_spec(value)


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
