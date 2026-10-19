"""
Lazarus Load Forecast and Worker Recommendation Module (app/forecast.py).
Implements PRD Section 9 N5:
- Arrival rate lambda: enqueued events in 5s buckets over last 120s.
- Holt's linear exponential smoothing (alpha=0.4, beta=0.2).
- Service time s = mean duration_ms of last 100 successes.
- Backlog = ready queued jobs.
- Recommended = clamp(ceil((lambda_hat * s + backlog * s / 30) / c * 1.2), 1, 8).
"""

import math
from typing import Dict, List, Tuple
import psycopg
from psycopg.rows import dict_row

from app.config import settings

SQL_ARRIVAL_BUCKETS = """
SELECT
  date_trunc('second', at) - make_interval(secs => (EXTRACT(SECOND FROM at)::int % 5)::float8) as bucket,
  count(*) as count
FROM job_events
WHERE event = 'enqueued' AND at >= now() - interval '120 seconds'
GROUP BY bucket
ORDER BY bucket ASC;
"""

SQL_MEAN_DURATION = """
SELECT COALESCE(AVG(duration_ms), 500) as mean_duration
FROM (
  SELECT duration_ms FROM jobs
  WHERE status = 'succeeded' AND duration_ms IS NOT NULL
  ORDER BY finished_at DESC
  LIMIT 100
) t;
"""

SQL_READY_BACKLOG = """
SELECT count(*) as backlog FROM jobs
WHERE status = 'queued' AND run_at <= now();
"""


def holt_linear_smoothing(
    series: List[float], alpha: float = 0.4, beta: float = 0.2
) -> float:
    """
    Computes Holt's linear exponential smoothing and forecasts 1-step ahead.
    """
    if not series:
        return 0.0
    if len(series) == 1:
        return series[0]

    level = series[0]
    trend = series[1] - series[0]

    for y in series[1:]:
        last_level = level
        level = alpha * y + (1 - alpha) * (level + trend)
        trend = beta * (level - last_level) + (1 - beta) * trend

    # One step ahead forecast
    return max(0.0, level + trend)


async def compute_recommendation(conn: psycopg.AsyncConnection) -> Dict:
    """
    Computes arrival forecast and recommended worker count (1-8).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        # 1) Arrival rate in 5s buckets
        await cur.execute(SQL_ARRIVAL_BUCKETS)
        buckets = await cur.fetchall()
        counts = [float(b["count"]) / 5.0 for b in buckets]  # jobs per second
        lambda_hat = holt_linear_smoothing(counts)

        # 2) Mean duration (service time s in seconds)
        await cur.execute(SQL_MEAN_DURATION)
        dur_row = await cur.fetchone()
        mean_dur_ms = float(dur_row["mean_duration"] if dur_row and dur_row["mean_duration"] is not None else 500.0)
        service_time_s = mean_dur_ms / 1000.0

        # 3) Backlog
        await cur.execute(SQL_READY_BACKLOG)
        b_row = await cur.fetchone()
        backlog = b_row["backlog"] or 0

        # 4) Recommendation formula
        # recommended = clamp(ceil((lambda_hat * s + backlog * s / 30) / c * 1.2), 1, 8)
        c = float(settings.worker_concurrency)
        target_drain_time = 30.0
        workload = (lambda_hat * service_time_s) + (backlog * service_time_s / target_drain_time)
        required_workers = math.ceil((workload / c) * 1.2)
        recommended = max(1, min(8, required_workers))

        return {
            "forecast_arrival_rate": round(lambda_hat, 2),
            "service_time_s": round(service_time_s, 3),
            "backlog": backlog,
            "recommended_workers": recommended,
            "concurrency_per_worker": int(c),
            "series": [round(c, 2) for c in counts[-24:]],
        }
