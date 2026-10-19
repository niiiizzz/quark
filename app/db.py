"""
Lazarus database connectivity module.
Raw SQL via psycopg 3 async pool. No ORM.
Windows event loop compatibility enforced.
"""

import sys
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

# Enforce selector event loop on Windows for psycopg async compatibility
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_pool: Optional[AsyncConnectionPool] = None


def create_pool(min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    """Create a new async connection pool for this process. Never shared across processes."""
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={
            "row_factory": dict_row,
            "autocommit": False,
        },
        open=False,
    )


async def get_pool() -> AsyncConnectionPool:
    """Get or initialize the process-local connection pool."""
    global _pool
    if _pool is None:
        _pool = create_pool()
        await _pool.open()
    return _pool


async def close_pool() -> None:
    """Close the process-local connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_connection() -> AsyncIterator[psycopg.AsyncConnection]:
    """Provide a connection from the pool."""
    pool = await get_pool()
    async with pool.connection() as conn:
        yield conn
