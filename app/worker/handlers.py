"""
Lazarus Job Handlers Module (app/worker/handlers.py).
Implements the exact handlers defined in PRD Section 8:
- ledger_credit: Sleeps 1-4s, buffers ledger row, committed atomically with fenced update.
- flaky_webhook: 0.3-1s, 15% random HTTP 503. If outage_flaky_webhook=true, 100% 503.
- validate_order: 30% raise ValidationError:missing_field -> permanent DLQ.
- legacy_sync: Payload bad:true (15%) -> ConnectionResetError (looks transient, learned as permanent).
- report_export: 1s normally; 5% take 10x longer (slow-job anomaly).
- poison_pill: os._exit(1) inside handler for silent worker crash -> quarantine test.
"""

import asyncio
import os
import random
from typing import Any, Dict, List, Optional


class ValidationError(Exception):
    """Permanent validation failure."""
    pass


class HandlerContext:
    """Context passed to job handlers."""

    def __init__(self, job: Dict[str, Any], settings_map: Dict[str, str]):
        self.job = job
        self.settings = settings_map
        self.effects: List[Dict[str, Any]] = []

    def effect(self, account: str, amount: float) -> None:
        """Buffer a ledger write that will be committed atomically with the fenced update."""
        self.effects.append({
            "tenant_id": self.job["tenant_id"],
            "account": account,
            "amount": amount,
        })


async def handle_ledger_credit(ctx: HandlerContext) -> Dict[str, Any]:
    """
    Sleeps 1-4s (long enough to be hit by kills/freezes),
    then buffers a ledger write.
    """
    sleep_time = random.uniform(1.0, 3.5)
    await asyncio.sleep(sleep_time)

    payload = ctx.job.get("payload") or {}
    account = payload.get("account", "ACC-DEFAULT")
    amount = float(payload.get("amount", 100.0))

    ctx.effect(account, amount)
    return {"credited_account": account, "amount": amount, "sleep_time": round(sleep_time, 2)}


async def handle_flaky_webhook(ctx: HandlerContext) -> Dict[str, Any]:
    """
    0.3-1s duration. 15% random HTTP 503.
    If outage_flaky_webhook=true in settings: 100% HTTP 503.
    """
    await asyncio.sleep(random.uniform(0.3, 0.8))

    outage = ctx.settings.get("outage_flaky_webhook", "false").lower() == "true"
    if outage or random.random() < 0.15:
        # Simulate transient downstream HTTP 503
        err = Exception("Downstream service returned HTTP 503 Service Unavailable")
        err.status_code = 503
        raise err

    return {"status": 200, "message": "webhook delivered"}


async def handle_validate_order(ctx: HandlerContext) -> Dict[str, Any]:
    """
    30% of payloads raise ValidationError:missing_field (permanent error -> DLQ).
    """
    await asyncio.sleep(random.uniform(0.1, 0.3))
    payload = ctx.job.get("payload") or {}

    # Check payload or induce 30% validation error if payload requests it or randomly
    if payload.get("invalid", False) or random.random() < 0.30:
        raise ValidationError("ValidationError:missing_field in order specification")

    return {"valid": True, "order_id": payload.get("order_id", "ORD-123")}


async def handle_legacy_sync(ctx: HandlerContext) -> Dict[str, Any]:
    """
    Payload bad:true (15% of jobs) -> always ConnectionResetError (looks transient,
    learned as permanent via Beta posterior).
    """
    await asyncio.sleep(random.uniform(0.2, 0.5))
    payload = ctx.job.get("payload") or {}

    if payload.get("bad", False):
        raise ConnectionResetError("Connection reset by legacy server peer: ECONNRESET")

    return {"synced": True, "records_processed": 42}


async def handle_report_export(ctx: HandlerContext) -> Dict[str, Any]:
    """
    1s normally; 5% take 10x longer (for slow-job anomaly demonstration).
    """
    is_slow = random.random() < 0.05
    duration = 5.0 if is_slow else 0.8
    await asyncio.sleep(duration)
    return {"pages": 12, "is_slow": is_slow, "duration": duration}


async def handle_poison_pill(ctx: HandlerContext) -> Dict[str, Any]:
    """
    Silent worker crash via os._exit(1).
    Simulates memory exhaustion / segfault where no exception handler runs.
    Tested via reaper crash_count -> quarantine.
    """
    await asyncio.sleep(0.2)
    # Silent suicide of the worker process!
    os._exit(1)


HANDLERS = {
    "ledger_credit": handle_ledger_credit,
    "flaky_webhook": handle_flaky_webhook,
    "validate_order": handle_validate_order,
    "legacy_sync": handle_legacy_sync,
    "report_export": handle_report_export,
    "poison_pill": handle_poison_pill,
}
