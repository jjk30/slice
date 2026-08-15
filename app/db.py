import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import asyncpg

logger = logging.getLogger("slice.gateway")

# Idempotent .sql files applied after the base table on every connect. Keeping
# them as files (rather than inline strings) makes each schema change a small,
# reviewable, ordered artifact.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    model         TEXT,
    status        INTEGER          NOT NULL,
    latency_ms    DOUBLE PRECISION NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      NUMERIC(14, 6),
    stream        BOOLEAN          NOT NULL
)
"""

INSERT = """
INSERT INTO requests
    (model, status, latency_ms, input_tokens, output_tokens, cost_usd, stream, cached)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""


@dataclass(frozen=True)
class RequestRecord:
    model: str | None
    status: int
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    stream: bool
    # True for a row that was served from the Redis cache (phase 4).
    cached: bool = False


class Database:
    """Fire-and-forget writer for the requests table.

    Every failure is swallowed and logged. A row is never worth a request, so
    this class has no path that raises into the caller.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5, timeout: float = 5.0):
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._pool: asyncpg.Pool | None = None

    @property
    def enabled(self) -> bool:
        return self._pool is not None

    async def connect(self) -> bool:
        """Open the pool and create the table. False means logging stays off."""
        try:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                command_timeout=self._timeout,
            )
            async with self._pool.acquire() as connection:
                await connection.execute(SCHEMA)
                await self._run_migrations(connection)
        except Exception as exc:
            await self.close()
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "connect", "error": str(exc)})
            )
            return False

        return True

    @staticmethod
    async def _run_migrations(connection) -> None:
        """Apply every migration file in order. Each is idempotent on its own."""
        if not MIGRATIONS_DIR.is_dir():
            return
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            await connection.execute(path.read_text())

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await pool.close()
        except Exception:
            pass

    async def record(self, record: RequestRecord) -> None:
        if self._pool is None:
            return

        try:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    INSERT,
                    record.model,
                    record.status,
                    record.latency_ms,
                    record.input_tokens,
                    record.output_tokens,
                    record.cost_usd,
                    record.stream,
                    record.cached,
                )
        except Exception as exc:
            # The response is already out the door; note it and drop the row.
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "write", "error": str(exc)})
            )
