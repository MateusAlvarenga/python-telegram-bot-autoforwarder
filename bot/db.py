from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import asyncpg


def normalize_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    if parsed.scheme == "postgres":
        return urlunsplit(("postgresql", *parsed[1:]))
    return database_url


class Database:
    def __init__(self, database_url: str) -> None:
        self._database_url = normalize_database_url(database_url)
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=5)
        async with self._pool.acquire() as connection:
            schema_statements = (
                """
                CREATE TABLE IF NOT EXISTS routines (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_chat_id TEXT NOT NULL,
                    source_topic_id BIGINT,
                    destination_chat_id TEXT NOT NULL,
                    destination_topic_id BIGINT,
                    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
                    last_message_id BIGINT NOT NULL DEFAULT 0,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    next_run_at TIMESTAMPTZ,
                    last_run_at TIMESTAMPTZ,
                    lease_token UUID,
                    lease_expires_at TIMESTAMPTZ
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS routines_due_idx
                    ON routines (next_run_at)
                    WHERE enabled = TRUE
                """,
                """
                CREATE TABLE IF NOT EXISTS routine_runs (
                    id BIGSERIAL PRIMARY KEY,
                    routine_id BIGINT NOT NULL,
                    routine_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    messages_copied INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS routine_runs_history_idx
                    ON routine_runs (routine_id, started_at DESC);
                """,
            )
            for statement in schema_statements:
                await connection.execute(statement)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database is not connected.")
        return self._pool

    async def list_routines(self) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch("SELECT * FROM routines ORDER BY id")
        return [dict(row) for row in rows]

    async def get_routine(self, routine_id: int) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM routines WHERE id = $1", routine_id)
        return dict(row) if row is not None else None

    async def create_routine(
        self,
        name: str,
        source_chat_id: str,
        source_topic_id: int | None,
        destination_chat_id: str,
        destination_topic_id: int | None,
        interval_seconds: int,
    ) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO routines (
                    name, source_chat_id, source_topic_id, destination_chat_id,
                    destination_topic_id, interval_seconds
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                name,
                source_chat_id,
                source_topic_id,
                destination_chat_id,
                destination_topic_id,
                interval_seconds,
            )
        return dict(row)

    async def update_routine(
        self,
        routine_id: int,
        name: str,
        source_chat_id: str,
        source_topic_id: int | None,
        destination_chat_id: str,
        destination_topic_id: int | None,
        interval_seconds: int,
    ) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE routines
                SET name = $2,
                    last_message_id = CASE
                        WHEN source_chat_id <> $3 OR source_topic_id IS DISTINCT FROM $4 THEN 0
                        ELSE last_message_id
                    END,
                    source_chat_id = $3,
                    source_topic_id = $4,
                    destination_chat_id = $5,
                    destination_topic_id = $6,
                    interval_seconds = $7,
                    next_run_at = CASE
                        WHEN enabled THEN NOW() + ($7::double precision * INTERVAL '1 second')
                        ELSE NULL
                    END,
                    updated_at = NOW()
                WHERE id = $1
                  AND (lease_token IS NULL OR lease_expires_at < NOW())
                RETURNING *
                """,
                routine_id,
                name,
                source_chat_id,
                source_topic_id,
                destination_chat_id,
                destination_topic_id,
                interval_seconds,
            )
        return dict(row) if row is not None else None

    async def delete_routine(self, routine_id: int) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                DELETE FROM routines
                WHERE id = $1
                  AND (lease_token IS NULL OR lease_expires_at < NOW())
                RETURNING id
                """,
                routine_id,
            )
        return row is not None

    async def set_routine_enabled(self, routine_id: int, enabled: bool) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE routines
                SET enabled = $2,
                    next_run_at = CASE WHEN $2 THEN NOW() ELSE NULL END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                routine_id,
                enabled,
            )
        return dict(row) if row is not None else None

    async def set_routine_cursor(
        self,
        routine_id: int,
        last_message_id: int,
    ) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE routines
                SET last_message_id = $2,
                    updated_at = NOW()
                WHERE id = $1
                  AND (lease_token IS NULL OR lease_expires_at < NOW())
                RETURNING *
                """,
                routine_id,
                last_message_id,
            )
        return dict(row) if row is not None else None

    async def get_due_routines(self) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM routines
                WHERE enabled = TRUE
                  AND next_run_at <= NOW()
                  AND (lease_token IS NULL OR lease_expires_at < NOW())
                ORDER BY next_run_at, id
                """
            )
        return [dict(row) for row in rows]

    async def claim_routine(
        self,
        routine_id: int,
        lease_token: UUID,
        manual: bool,
        lease_seconds: int,
    ) -> tuple[dict[str, Any], int] | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE routines
                    SET lease_token = $2,
                        lease_expires_at = NOW() + ($4::double precision * INTERVAL '1 second')
                    WHERE id = $1
                      AND (lease_token IS NULL OR lease_expires_at < NOW())
                      AND (enabled = TRUE OR $3 = TRUE)
                    RETURNING *
                    """,
                    routine_id,
                    lease_token,
                    manual,
                    lease_seconds,
                )
                if row is None:
                    return None
                run_id = await connection.fetchval(
                    """
                    INSERT INTO routine_runs (routine_id, routine_name, status)
                    VALUES ($1, $2, 'running')
                    RETURNING id
                    """,
                    routine_id,
                    row["name"],
                )
        return dict(row), int(run_id)

    async def save_cursor(
        self,
        routine_id: int,
        lease_token: UUID,
        last_message_id: int,
        lease_seconds: int,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            updated_id = await connection.fetchval(
                """
                UPDATE routines
                SET last_message_id = $3,
                    lease_expires_at = NOW() + ($4::double precision * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE id = $1 AND lease_token = $2
                RETURNING id
                """,
                routine_id,
                lease_token,
                last_message_id,
                lease_seconds,
            )
        if updated_id is None:
            raise RuntimeError(f"Lost the execution lease for routine {routine_id}.")

    async def finish_run(
        self,
        routine_id: int,
        run_id: int,
        lease_token: UUID,
        status: str,
        messages_copied: int,
        error: str | None,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE routine_runs
                    SET status = $2,
                        finished_at = NOW(),
                        messages_copied = $3,
                        error = $4
                    WHERE id = $1
                    """,
                    run_id,
                    status,
                    messages_copied,
                    error,
                )
                updated_id = await connection.fetchval(
                    """
                    UPDATE routines
                    SET lease_token = NULL,
                        lease_expires_at = NULL,
                        last_run_at = NOW(),
                        next_run_at = CASE
                            WHEN enabled THEN NOW() + (
                                interval_seconds::double precision * INTERVAL '1 second'
                            )
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE id = $1 AND lease_token = $2
                    RETURNING id
                    """,
                    routine_id,
                    lease_token,
                )
                if updated_id is None:
                    raise RuntimeError(f"Lost the execution lease for routine {routine_id}.")

    async def list_runs(self, routine_id: int, limit: int = 5) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM routine_runs
                WHERE routine_id = $1
                ORDER BY started_at DESC
                LIMIT $2
                """,
                routine_id,
                limit,
            )
        return [dict(row) for row in rows]
