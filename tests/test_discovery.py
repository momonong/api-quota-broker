"""Discovery evidence storage contracts; sources use normalized offline fixtures."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quota_broker.core import stamp
from quota_broker.discovery import DiscoveryError, DiscoveryStore
from quota_broker.registry import Registry

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
SOURCE = "https://docs.api.nvidia.com/models"
OTHER_SOURCE = "https://build.nvidia.com/catalog"


def model(name="fixture-model", capability="text_generation", **changes):
    if changes.get("free_eligibility", "unknown") != "unknown":
        changes.setdefault("free_source", SOURCE)
    return {
        "model": name,
        "capability": capability,
        "hosting": "hosted",
        "endpoint": "https://integrate.api.nvidia.com/v1/chat/completions",
        "protocol": "openai_chat",
        "free_eligibility": "unknown",
        "free_source": None,
        "status": "listed",
        "context_tokens": 8192,
        "max_output_tokens": 1024,
        "features": ["text"],
        **changes,
    }


def snapshot(models=None, *, provider="nvidia", source=SOURCE, at=NOW, complete=True, **extra):
    models = [model()] if models is None else models
    if provider != "nvidia" and source == SOURCE:
        source = {
            "google": "https://ai.google.dev/models",
            "groq": "https://console.groq.com/models",
        }[provider]
        endpoint = {
            "google": "https://generativelanguage.googleapis.com/v1/models",
            "groq": "https://api.groq.com/openai/v1/chat/completions",
        }[provider]
        models = [{**item, "endpoint": endpoint} for item in models]
    return {
        "schema_version": 1,
        "provider": provider,
        "source": source,
        "checked_at": stamp(at),
        "complete": complete,
        "models": models,
        **extra,
    }


def attestation(*, at=NOW, until=None, live_at=None, **changes):
    return {
        "provider": "nvidia",
        "model": "fixture-model",
        "capability": "text_generation",
        "account_availability": "allowed",
        "account_scope": "opaque-fixture-account",
        "account_checked_at": stamp(at),
        "account_valid_until": stamp(until or at + timedelta(minutes=10)),
        "live_result": "unverified",
        "live_checked_at": stamp(live_at) if live_at else None,
        "receipt_id": "fixture-receipt" if live_at else None,
        **changes,
    }


@pytest.fixture
def clock():
    return [NOW + timedelta(seconds=4)]


@pytest.fixture
def store(tmp_path, clock):
    return DiscoveryStore(tmp_path / "discovery.sqlite", Registry.builtin(), clock=lambda: clock[0])


def test_restart_and_same_snapshot_are_idempotent(store):
    first = store.refresh(snapshot())
    assert first["applied"] and first["added"] == [
        {"provider": "nvidia", "model": "fixture-model", "capability": "text_generation"}
    ]
    repeated = store.refresh(snapshot())
    assert repeated["idempotent"] and not repeated["applied"]
    restarted = DiscoveryStore(store.db, store.registry, clock=store.clock)
    assert restarted.lookup("nvidia", "fixture-model", "text_generation")["source"] == SOURCE
    assert restarted.refresh(snapshot())["snapshot_id"] == first["snapshot_id"]
    with sqlite3.connect(store.db) as con:
        assert con.execute("SELECT count(*) FROM discovery_snapshots").fetchone()[0] == 1


def test_partial_complete_missing_and_explicit_retirement_preserve_history(store):
    store.refresh(snapshot([model("alpha"), model("beta")]))
    partial = store.refresh(
        snapshot(
            [model("alpha", context_tokens=16384)], at=NOW + timedelta(seconds=1), complete=False
        )
    )
    assert len(partial["changed"]) == 1 and partial["no_longer_listed"] == []
    assert store.lookup("nvidia", "beta", "text_generation")["status"] == "listed"
    assert (
        store.coverage(provider="nvidia")["providers"][0]["source_catalog_completeness"]
        == "partial"
    )
    retired = store.refresh(
        snapshot([model("alpha", status="retired")], at=NOW + timedelta(seconds=2))
    )
    assert retired["explicitly_retired"] == [
        {"provider": "nvidia", "model": "alpha", "capability": "text_generation"}
    ]
    assert retired["no_longer_listed"] == [
        {"provider": "nvidia", "model": "beta", "capability": "text_generation"}
    ]
    missing = store.lookup("nvidia", "beta", "text_generation")
    assert missing["status"] == "not_listed"
    assert missing["sources"][0]["declared_status"] == "listed"
    assert missing["checked_at"] == stamp(NOW)
    assert missing["listing_checked_at"] == stamp(NOW + timedelta(seconds=2))
    with sqlite3.connect(store.db) as con:
        history = [
            json.loads(row[0])
            for row in con.execute("SELECT diff_json FROM discovery_snapshots ORDER BY checked_at")
        ]
    assert len(history) == 3 and history[-1]["explicitly_retired"] == retired["explicitly_retired"]


def test_stale_and_equal_time_conflicts_cannot_overwrite_newer_evidence(store):
    store.refresh(snapshot([model(free_eligibility="paid")], at=NOW + timedelta(seconds=3)))
    stale = store.refresh(snapshot([model(free_eligibility="free")]))
    equal = store.refresh(snapshot([model(free_eligibility="free")], at=NOW + timedelta(seconds=3)))
    assert stale["stale"] and equal["stale"] and not stale["applied"]
    assert store.lookup("nvidia", "fixture-model", "text_generation")["free_eligibility"] == "paid"
    with sqlite3.connect(store.db) as con:
        assert con.execute("SELECT count(*) FROM discovery_snapshots").fetchone()[0] == 3


def test_complete_absence_is_scoped_to_one_source(store):
    store.refresh(snapshot())
    store.refresh(snapshot(source=OTHER_SOURCE, at=NOW + timedelta(seconds=1)))
    diff = store.refresh(snapshot([model("different")], at=NOW + timedelta(seconds=2)))
    assert len(diff["no_longer_listed"]) == 1
    evidence = {
        row["source"]: row
        for row in store.lookup("nvidia", "fixture-model", "text_generation")["sources"]
    }
    assert evidence[SOURCE]["status"] == "not_listed"
    assert evidence[OTHER_SOURCE]["status"] == "listed"
    assert evidence[OTHER_SOURCE]["free_eligibility"] == "unknown"


def test_provider_and_capability_identity_are_independent(store):
    store.refresh(
        snapshot([model("same"), model("same", "speech_to_text", protocol=None)], provider="nvidia")
    )
    store.refresh(snapshot([model("same")], provider="groq"))
    summary = store.coverage(model="same")["summary"]
    assert summary["unique_models"] == 2 and summary["model_capability_records"] == 3
    assert summary["total"] == 3
    assert store.lookup("nvidia", "same", "speech_to_text")["protocol"] is None
    assert store.lookup("groq", "same", "text_generation")["provider"] == "groq"


def test_catalog_refresh_does_not_modify_account_or_live_and_expiry_is_read_only(store, clock):
    store.refresh(snapshot())
    proof = store.attest(
        attestation(live_at=NOW, live_result="passed", receipt_id="fixture-receipt")
    )
    assert proof["account_availability"] == "allowed" and proof["live_result"] == "passed"
    store.refresh(
        snapshot([model(free_eligibility="paid", status="retired")], at=NOW + timedelta(seconds=1))
    )
    record = store.lookup("nvidia", "fixture-model", "text_generation")
    assert record["account_availability"] == "allowed" and record["live_result"] == "passed"
    before = hashlib.sha256(Path(store.db).read_bytes()).hexdigest()
    clock[0] += timedelta(minutes=11)
    expired = store.lookup("nvidia", "fixture-model", "text_generation")
    assert expired["account_availability"] == "unknown"
    assert expired["accounts"][0]["declared_availability"] == "allowed"
    assert expired["live_result"] == "passed" and expired["live_checked_at"] == stamp(NOW)
    assert hashlib.sha256(Path(store.db).read_bytes()).hexdigest() == before


def test_account_and_live_timestamp_updates_are_independent_and_history_is_preserved(store, clock):
    store.attest(attestation(live_at=NOW, live_result="passed", receipt_id="older-receipt"))
    clock[0] = NOW + timedelta(minutes=1)
    update = attestation(at=NOW + timedelta(minutes=1), account_availability="blocked")
    blocked = store.attest(update)
    assert blocked["account_availability"] == "blocked" and blocked["live_result"] == "passed"
    # Older account evidence may contain a newer independent live observation.
    clock[0] = NOW + timedelta(minutes=2)
    newer_live = store.attest(
        attestation(
            at=NOW - timedelta(minutes=1),
            live_at=NOW + timedelta(minutes=2),
            live_result="failed",
            receipt_id="later-receipt",
        )
    )
    assert newer_live["account_availability"] == "blocked" and newer_live["live_result"] == "failed"
    assert store.attest(update)["idempotent"]
    with sqlite3.connect(store.db) as con:
        assert con.execute("SELECT count(*) FROM discovery_attestations").fetchone()[0] == 3


def test_account_scopes_are_preserved_without_duplicating_public_identity(store, clock):
    store.attest(attestation(account_scope="one", account_availability="blocked"))
    store.attest(attestation(account_scope="two", until=NOW + timedelta(minutes=1)))
    known = store.lookup("nvidia", "fixture-model", "text_generation")
    assert len(known["accounts"]) == 2 and known["account_availability"] == "allowed"
    clock[0] += timedelta(minutes=2)
    unknown = store.lookup("nvidia", "fixture-model", "text_generation")
    assert unknown["account_availability"] == "unknown"
    assert store.coverage(provider="nvidia", model="fixture-model")["summary"]["total"] == 1


def test_support_is_exact_registry_or_compatible_not_catalog_or_live_inference(store):
    store.refresh(
        snapshot(
            [
                model("gemini-3.5-flash-lite"),
                model("new-chat"),
                model("new-embedding", "embedding"),
                model("novel-protocol", protocol="new_protocol"),
                model("unknown", protocol=None),
            ],
            provider="google",
        )
    )
    assert (
        store.lookup("google", "gemini-3.5-flash-lite", "text_generation")["adapter_support"]
        == "supported"
    )
    assert (
        store.lookup("google", "new-chat", "text_generation")["adapter_support"]
        == "compatible_unregistered"
    )
    assert store.lookup("google", "new-embedding", "embedding")["adapter_support"] == "unsupported"
    assert (
        store.lookup("google", "novel-protocol", "text_generation")["adapter_support"]
        == "unsupported"
    )
    assert store.lookup("google", "unknown", "text_generation")["adapter_support"] == "unknown"
    store.attest(
        attestation(provider="google", model="new-chat", live_result="passed", live_at=NOW)
    )
    assert (
        store.lookup("google", "new-chat", "text_generation")["adapter_support"]
        == "compatible_unregistered"
    )


def test_historical_registry_seeds_do_not_claim_official_free_or_live_evidence(store):
    coverage = store.coverage()
    assert len(coverage["providers"]) == 7
    assert all(
        provider["source_catalog_completeness"] == "unobserved"
        for provider in coverage["providers"]
    )
    assert all(
        record["source_kind"] == "registry_historical"
        and record["source"] is None
        and record["free_eligibility"] == "unknown"
        and record["account_availability"] == "unknown"
        and record["live_result"] == "unverified"
        for record in coverage["records"]
    )
    assert coverage["denominator"]["state"] == "unknown" and coverage["coverage_percent"] is None


def test_stable_pagination_and_summary_cover_all_filtered_records(store):
    store.refresh(snapshot([model(f"model-{number}", "scientific_fixture") for number in range(5)]))
    first = store.coverage(provider="nvidia", capability="scientific_fixture", limit=2)
    second = store.coverage(
        provider="nvidia", capability="scientific_fixture", limit=2, before=first["next_before"]
    )
    third = store.coverage(
        provider="nvidia", capability="scientific_fixture", limit=2, before=second["next_before"]
    )
    assert [row["model"] for page in (first, second, third) for row in page["records"]] == [
        f"model-{number}" for number in range(5)
    ]
    assert all(page["summary"]["total"] == 5 for page in (first, second, third))
    assert third["next_before"] is None
    assert first["providers"][0]["source_catalog_completeness"] == "complete"
    assert first["coverage_percent"] is None
    assert first["providers"][0]["provider_capability_completeness"] == "unknown"
    with pytest.raises(DiscoveryError):
        store.coverage(provider="different", before=first["next_before"])
    for kwargs in ({"limit": 0}, {"limit": 1001}, {"before": "invalid!"}):
        with pytest.raises(DiscoveryError):
            store.coverage(**kwargs)


def test_sensitive_extra_transport_fields_are_rejected_without_persistence(store):
    before = Path(store.db).read_bytes()
    for raw in (
        snapshot([model(raw_response="sensitive fixture response")]),
        snapshot(raw_message="sensitive fixture prompt"),
        snapshot(authorization="sensitive fixture credential"),
    ):
        with pytest.raises(DiscoveryError):
            store.refresh(raw)
    with pytest.raises(DiscoveryError):
        store.attest(attestation(raw_message="sensitive fixture secret"))
    assert Path(store.db).read_bytes() == before
    assert b"sensitive" not in Path(store.db).read_bytes()
    assert "sensitive" not in json.dumps(store.coverage())


def test_lookup_missing_and_unknown_provider_counts_do_not_invent_evidence(store):
    with pytest.raises(DiscoveryError) as error:
        store.record("not-listed", "missing", "scientific_forecast")
    assert error.value.code == "not_found"
    empty = store.coverage(provider="not-listed")
    assert empty["records"] == [] and empty["summary"]["total"] == 0
    assert empty["providers"][0]["source_catalog_completeness"] == "unobserved"


def test_same_scope_readiness_intersection_and_expiry_preserve_live_history(store, clock):
    store.refresh(snapshot([model(free_eligibility="free")]))
    store.attest(attestation(account_scope="account-a"))
    store.attest(
        attestation(
            account_scope="account-b",
            account_availability="blocked",
            live_at=NOW,
            live_result="passed",
        )
    )
    record = store.lookup("nvidia", "fixture-model", "text_generation")
    assert record["account_availability"] == "allowed" and record["live_result"] == "passed"
    assert not record["evidence_eligible"] and not record["evidence_ready"]
    assert record["accounts"][1]["live_result"] == "passed"
    assert record["free_checked_at"] == stamp(NOW)
    clock[0] += timedelta(seconds=1)
    store.attest(
        attestation(account_scope="account-b", at=clock[0], live_at=NOW, live_result="passed")
    )
    assert store.lookup("nvidia", "fixture-model", "text_generation")["evidence_eligible"]
    assert (
        store.coverage(provider="nvidia", model="fixture-model")["summary"]["evidence_eligible"]
        == 1
    )
    clock[0] += timedelta(minutes=11)
    expired = store.lookup("nvidia", "fixture-model", "text_generation")
    assert expired["live_result"] == "passed" and not expired["evidence_eligible"]
    assert (
        store.coverage(provider="nvidia", model="fixture-model")["summary"]["evidence_eligible"]
        == 0
    )
    assert expired["readiness_basis"].endswith("not_runtime_admission")


def test_ready_evidence_requires_dated_free_catalog_and_exact_registered_model(store):
    registered = next(
        spec
        for spec in store.registry.models.values()
        if spec.provider == "nvidia" and spec.capability == "text_generation"
    )
    store.refresh(snapshot([model(registered.model, free_eligibility="free")]))
    initial = store.lookup("nvidia", registered.model, "text_generation")
    assert initial["adapter_support"] == "supported"
    assert not initial["evidence_eligible"] and not initial["evidence_ready"]
    proof = store.attest(attestation(model=registered.model, live_at=NOW, live_result="passed"))
    assert proof["evidence_eligible"] and proof["evidence_ready"]
    assert proof["free_checked_at"] == stamp(NOW)
    assert proof["configured_target_ids"] == []
    store.refresh(
        snapshot(
            [model(registered.model, free_eligibility="free", status="retired")],
            at=NOW + timedelta(seconds=1),
        )
    )
    retired = store.lookup("nvidia", registered.model, "text_generation")
    assert retired["evidence_eligible"] and not retired["evidence_ready"]
