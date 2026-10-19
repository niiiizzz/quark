"""
Lazarus Retry, Fingerprinting, and Classification Module (app/retry.py).
Implements:
- Capped exponential backoff with full jitter (PRD C5 / Section 8).
- Error fingerprinting (PRD Section 9 N3).
- Rule-based prior classification (ValidationError, 4xx -> permanent).
- Statistical learning via Beta posterior:
  p = (1 + successes) / (2 + successes + failures)
  Learned-permanent when n >= 8 and p < 0.15.
  Guarded when circuit breaker is open.
"""

import math
import random
from typing import Optional, Tuple
import psycopg
from psycopg.rows import dict_row

from app.config import settings


def compute_backoff(attempt: int, base: float = 1.5, cap: float = 20.0) -> float:
    """
    PRD C5 / §8: Uniform delay in [0, min(cap, base * 2^attempt)].
    Full jitter prevents thundering herds.
    """
    max_delay = min(cap, base * (2 ** attempt))
    return random.uniform(0.0, max_delay)


def extract_fingerprint(exc: Exception, http_status: Optional[int] = None) -> str:
    """
    PRD §9 N3: fp = f"{ExceptionClass}:{http_status_or_code}".
    """
    cls_name = exc.__class__.__name__
    if http_status is not None:
        return f"{cls_name}:{http_status}"
    if hasattr(exc, "status_code"):
        return f"{cls_name}:{getattr(exc, 'status_code')}"
    return f"{cls_name}:0"


def classify_error_prior(exc: Exception, http_status: Optional[int] = None) -> Tuple[bool, str]:
    """
    Rule prior:
    ValidationError or HTTP 4xx (except 408 Request Timeout, 429 Too Many Requests) -> permanent.
    Everything else -> transient.
    Returns (is_permanent, error_class).
    """
    cls_name = exc.__class__.__name__
    if "ValidationError" in cls_name:
        return True, "permanent"

    status = http_status or getattr(exc, "status_code", None)
    if status is not None and isinstance(status, int):
        if 400 <= status < 500 and status not in (408, 429):
            return True, "permanent"

    return False, "transient"


async def classify_and_record_retry(
    conn: psycopg.AsyncConnection,
    job_type: str,
    fp: str,
    is_success: bool,
    breaker_is_open: bool = False,
) -> Tuple[bool, str, float]:
    """
    Updates observation counts for (job_type, fp).
    Calculates Beta posterior p = (1 + s) / (2 + s + f).
    Learned permanent if n >= 8 and p < 0.15 (suspended if breaker is open).
    Returns (is_permanent, error_class, p_success).
    """
    if breaker_is_open:
        # Guard: learning is suspended during outage
        return False, "transient", 0.5

    async with conn.cursor(row_factory=dict_row) as cur:
        if is_success:
            await cur.execute(
                """
                INSERT INTO retry_stats (job_type, fp, successes, failures)
                VALUES (%s, %s, 1, 0)
                ON CONFLICT (job_type, fp) DO UPDATE
                SET successes = retry_stats.successes + 1
                RETURNING successes, failures;
                """,
                (job_type, fp),
            )
        else:
            await cur.execute(
                """
                INSERT INTO retry_stats (job_type, fp, successes, failures)
                VALUES (%s, %s, 0, 1)
                ON CONFLICT (job_type, fp) DO UPDATE
                SET failures = retry_stats.failures + 1
                RETURNING successes, failures;
                """,
                (job_type, fp),
            )

        row = await cur.fetchone()
        s = row["successes"]
        f = row["failures"]
        n = s + f
        p = (1.0 + s) / (2.0 + s + f)

        if n >= 8 and p < 0.15:
            return True, "learned_permanent", p

        return False, "transient", p
