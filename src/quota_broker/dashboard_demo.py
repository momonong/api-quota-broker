"""Synthetic evidence backed by the actual routing engine; provider I/O is forbidden."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Evidence, Quota, QuotaFact, SecretInventory, Target
from .core import utcnow
from .dashboard import GatewaySource
from .gateway import Gateway


def _forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("offline demo forbids credential resolution and provider transport")


def create_demo(directory: str | Path, *, clock: Callable[[], datetime] = utcnow) -> GatewaySource:
    now = clock()
    quotas = (
        Quota("demo-rpm", "requests", 20, "rolling_minute"),
        Quota("demo-rpd", "requests", 200, "day"),
        Quota("demo-tpm", "input_tokens", 12000, "rolling_minute"),
    )

    def evidence(value: int | None) -> Evidence:
        return Evidence(
            value, "official", now, "synthetic_fixture", "demo", now + timedelta(hours=1)
        )

    ready = Target(
        "demo-ready",
        "google",
        "gemini-3.5-flash-lite",
        "synthetic-account",
        True,
        True,
        False,
        now,
        now + timedelta(hours=1),
        quotas,
        3,
        256,
        0,
        "synthetic fixture only",
        quota_basis="local_safety_cap",
        secret_ref="DEMO_FAKE_NAME",
        provider_quota_facts=tuple(
            QuotaFact(
                q.metric,
                q.window,
                evidence(q.limit),
                evidence(
                    12 if q.window == "rolling_minute" and q.metric == "requests" else q.limit - 2
                ),
            )
            for q in quotas
        ),
    )
    unknown = replace(
        ready,
        id="demo-unknown",
        provider="groq",
        model="openai/gpt-oss-20b",
        priority=1,
        quotas=tuple(replace(q, bucket=q.bucket + "-unknown") for q in quotas),
        provider_quota_facts=(QuotaFact("requests", "day", evidence(None), evidence(None)),),
    )
    stale = replace(
        ready,
        id="demo-recheck",
        provider="mistral",
        model="ministral-3b-latest",
        priority=2,
        expires_at=now - timedelta(minutes=1),
        quotas=tuple(replace(q, bucket=q.bucket + "-recheck") for q in quotas),
    )
    gateway = Gateway(
        Path(directory) / "dashboard-demo.db",
        (ready, unknown, stale),
        b"synthetic-demo-digest-material-00000",
        _forbidden,
        _forbidden,
        clock=clock,
        secret_inventory=SecretInventory(
            frozenset({"DEMO_FAKE_NAME"}), now, now + timedelta(hours=1), "synthetic", "demo"
        ),
        family_transport=_forbidden,
    )
    return GatewaySource(gateway)
