"""
Lazarus Circuit Breaker Module (app/breaker.py).
Implements PRD Section 9 N4:
- Evaluated after every failure and by the control loop.
- Window: attempts finishing after breakers.window_start and within the last 20s.
- Open if samples >= 6 and failure rate >= 70%.
- On open: open_until = now() + 15s, window_start = open_until, opened_count++.
- Claims skip that type.
"""

from typing import Dict, List, Optional
import psycopg
from psycopg.rows import dict_row

SQL_EVAL_BREAKER_WINDOW = """
SELECT
  count(*) as total_samples,
  count(*) FILTER (WHERE event IN ('failed', 'dead')) as fail_samples
FROM job_events
WHERE job_type = %s
  AND at >= (
    SELECT COALESCE(MAX(window_start), now() - interval '20 seconds')
    FROM breakers WHERE job_type = %s
  )
  AND at >= now() - interval '20 seconds'
  AND event IN ('succeeded', 'failed', 'dead');
"""

SQL_OPEN_BREAKER = """
INSERT INTO breakers (job_type, open_until, window_start, opened_count)
VALUES (%s, now() + interval '15 seconds', now() + interval '15 seconds', 1)
ON CONFLICT (job_type) DO UPDATE
SET open_until = now() + interval '15 seconds',
    window_start = now() + interval '15 seconds',
    opened_count = breakers.opened_count + 1
RETURNING job_type, open_until, opened_count;
"""

SQL_GET_BREAKER_STATUS = """
SELECT job_type,
       open_until,
       CASE WHEN open_until > now() THEN 'open' ELSE 'closed' END as state,
       GREATEST(0, EXTRACT(EPOCH FROM (open_until - now())))::int as open_for_s,
       opened_count
FROM breakers;
"""


async def evaluate_breaker(conn: psycopg.AsyncConnection, job_type: str) -> bool:
    """
    Evaluates failure rate for job_type in the 20s window.
    Opens breaker if samples >= 6 and failure_rate >= 0.70.
    Returns True if breaker is open.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        # Check current state first
        await cur.execute(
            "SELECT open_until > now() AS is_open FROM breakers WHERE job_type = %s",
            (job_type,),
        )
        row = await cur.fetchone()
        if row and row["is_open"]:
            return True

        # Check window samples
        await cur.execute(SQL_EVAL_BREAKER_WINDOW, (job_type, job_type))
        stats = await cur.fetchone()
        if not stats or stats["total_samples"] < 6:
            return False

        failure_rate = stats["fail_samples"] / float(stats["total_samples"])
        if failure_rate >= 0.70:
            await cur.execute(SQL_OPEN_BREAKER, (job_type,))
            return True

        return False


async def get_all_breakers(conn: psycopg.AsyncConnection) -> List[Dict]:
    """Retrieve status of all known circuit breakers."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SQL_GET_BREAKER_STATUS)
        return await cur.fetchall()
