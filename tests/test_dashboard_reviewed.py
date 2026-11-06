"""Synthetic reviewed profiles retain uncertainty; every external path is forbidden."""

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quota_broker.config import ConfigError
from quota_broker.dashboard import Dashboard

ANCHOR = datetime(2026, 1, 3, 12, tzinfo=UTC)
DUE = ANCHOR + timedelta(days=14)
ROOT = Path(__file__).resolve().parents[1]


def write_bundle(tmp_path):
    directory = tmp_path / "reviewed"
    directory.mkdir(mode=0o700)
    originals = json.loads((ROOT / "gateway.example.json").read_text())["targets"]
    targets = []
    for provider in ("nvidia", "google", "groq", "mistral", "cloudflare", "openrouter", "ocrspace"):
        target = next(t for t in originals if t["provider"] == provider)
        target.update(
            enabled=False,
            billing_enabled=True,
            account_id="reviewed-local-placeholder",
            free_eligible=provider != "ocrspace",
            verified_at=ANCHOR.isoformat() if provider != "ocrspace" else None,
            expires_at=DUE.isoformat() if provider != "ocrspace" else None,
            source="Synthetic reviewed fixture",
            provider_quota_facts=[],
        )
        targets.append(target)
    registry = {"schema_version": 1, "providers": [], "models": []}
    for provider in ("sambanova", "huggingface", "cerebras"):
        registry["providers"].append(
            {
                "id": provider,
                "adapter": "openai_chat",
                "origin": "https://" + provider + ".example",
                "endpoint": "https://" + provider + ".example/v1/chat/completions",
            }
        )
        registry["models"].append(
            {
                "provider": provider,
                "model": provider + "-fixture",
                "capability": "text_generation",
                "context_tokens": 8192,
                "max_output_tokens": 1024,
                "features": ["text_generation", "text"],
            }
        )
        target = copy.deepcopy(targets[2])
        target.update(
            id=provider + "-fixture",
            provider=provider,
            model=provider + "-fixture",
            free_eligible=False,
            verified_at=None,
            expires_at=None,
        )
        target["local_safety_caps"] = [
            {
                "bucket": provider + "-" + window,
                "metric": "requests",
                "limit": 1,
                "window": window,
                "timezone": "UTC",
            }
            for window in ("rolling_minute", "day")
        ]
        targets.append(target)
    rows = []
    for target in targets:
        rows.append(
            {
                "target_id": target["id"],
                "qualification_basis": "account_plan" if target["free_eligible"] else "unverified",
                "billing_state": "unknown",
                "plan": "free" if target["free_eligible"] else "unknown",
                "notes": ["Synthetic account evidence"],
                "sources": ["https://build.nvidia.com/"],
                "limit_context": [],
                "display_facts": [],
                "derived_remaining": None,
            }
        )
        target["billing_enabled"] = True
    unknown = {
        "value": None,
        "provenance": "unknown",
        "as_of": None,
        "source": None,
        "scope": None,
        "valid_until": None,
    }

    def fact(metric, window, limit, remaining=None):
        evidence = {
            "value": limit,
            "provenance": "official",
            "as_of": ANCHOR.isoformat(),
            "source": "https://build.nvidia.com/",
            "scope": "account",
            "valid_until": DUE.isoformat(),
        }
        return {
            "metric": metric,
            "window": window,
            "limit": evidence,
            "remaining": copy.deepcopy(unknown)
            if remaining is None
            else {
                **evidence,
                "value": remaining,
                "provenance": "estimated",
                "valid_until": (ANCHOR + timedelta(minutes=15)).isoformat(),
            },
        }

    targets[1]["provider_quota_facts"] = [fact("requests", "day", 500)]
    rows[1]["limit_context"] = [
        {
            "metric": "requests",
            "window": "day",
            "basis": "account",
            "reset": "pacific_midnight_project",
        }
    ]
    targets[4]["provider_quota_facts"] = [fact("neurons", "day", 10000, 10000)]
    rows[4]["limit_context"] = [
        {"metric": "neurons", "window": "day", "basis": "account", "reset": "utc_midnight"}
    ]
    rows[4]["derived_remaining"] = {
        "metric": "neurons",
        "window": "day",
        "used": 0,
        "limit": 10000,
        "reset_at": (ANCHOR + timedelta(days=1)).replace(hour=0).isoformat(),
        "note": "derived_from_used_and_limit",
    }
    rows[3]["display_facts"] = [
        {
            "kind": "requests_per_second",
            "value": 12.5,
            "period": "per_second",
            "precision": "exact",
        },
        {
            "kind": "monthly_allowance_usd",
            "value": 10,
            "period": "calendar_month",
            "precision": "exact",
        },
        {
            "kind": "used_allowance_usd",
            "value": 0,
            "period": "calendar_month",
            "precision": "rounded",
        },
    ]
    rows[5]["qualification_basis"] = "public_conditions"
    profile = {
        "schema_version": 1,
        "kind": "manually_reviewed_account_snapshot",
        "audit_session_at": ANCHOR.isoformat(),
        "time_precision": "session_clock_not_per_provider_observation",
        "review_due_at": DUE.isoformat(),
        "targets": rows,
    }
    for name, data in (
        ("reviewed.json", profile),
        ("gateway.json", {"targets": targets}),
        ("registry.json", registry),
    ):
        path = directory / name
        path.write_text(json.dumps(data))
        path.chmod(0o600)
    return directory / "reviewed.json", profile, targets


def load_board(tmp_path, clock=lambda: ANCHOR):
    from quota_broker.dashboard_reviewed import create_reviewed

    config, profile, targets = write_bundle(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source = create_reviewed(runtime, config, clock=clock)
    return Dashboard(source, clock=clock), source, config, profile, targets


def test_reviewed_known_limits_never_become_remaining_or_recommendation(tmp_path):
    board, source, _config, _profile, _targets = load_board(tmp_path)
    snapshot = board.snapshot()
    assert snapshot["source"]["kind"] == "reviewed"
    assert "人工核對" in snapshot["source"]["label"]
    assert snapshot["source"]["audit_session_at"] == ANCHOR.isoformat()
    assert len(snapshot["targets"]) == 10
    google = next(t for t in snapshot["targets"] if t["provider"] == "google")
    assert google["provider_limits"][0]["value"] == 500
    assert google["provider_limits"][0]["basis"] == "account"
    assert google["observations"][0]["remaining"] is None
    assert google["billing_enabled"] is None
    assert google["billing_state"] == "unknown"
    assert all(
        t["credentials"]["state"] == "unknown" and t["enabled"] is False
        for t in snapshot["targets"]
    )
    result = board.advice(
        {
            "request_key": "synthetic-advice",
            "capability": "text_generation",
            "input": "fixture",
            "max_output_tokens": 16,
        }
    )
    assert result["decision"] == "ask_user" and result["selected_target"] is None
    assert result["execution_allowed"] is result["paid_allowed"] is False
    with sqlite3.connect(source.gateway.db) as con:
        for table in ("reservations", "gateway_tasks", "gateway_attempts", "observed_holds"):
            assert con.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    for path in (
        source.gateway.secret_resolver,
        source.gateway.transport,
        source.gateway.family_transport,
    ):
        with pytest.raises(AssertionError):
            path()


def test_derived_remaining_is_estimated_and_expired_without_renewal(tmp_path):
    from quota_broker.dashboard_reviewed import create_reviewed

    current = [ANCHOR]
    board, _source, config, _profile, _targets = load_board(tmp_path, lambda: current[0])

    def observation(board):
        return next(t for t in board.snapshot()["targets"] if t["provider"] == "cloudflare")[
            "observations"
        ][0]

    before = observation(board)
    assert before["remaining"] == 10000 and before["provenance"] == "estimated"
    assert before["current"] is True and before["remaining_basis"] == "derived_from_used_and_limit"
    assert before["reset_at"] is not None
    current[0] += timedelta(minutes=16)
    later = observation(board)
    assert later["remaining"] == 10000 and later["current"] is False
    assert later["effective_remaining"] is None
    runtime = tmp_path / "reload"
    runtime.mkdir()
    reloaded = Dashboard(
        create_reviewed(runtime, config, clock=lambda: current[0]), clock=lambda: current[0]
    )
    assert observation(reloaded) == later
    assert reloaded.snapshot()["source"]["audit_session_at"] == ANCHOR.isoformat()


def test_fractional_rps_usd_metadata_and_public_qualification_stay_separate(tmp_path):
    board, _source, _config, _profile, _targets = load_board(tmp_path)
    rows = {t["provider"]: t for t in board.snapshot()["targets"]}
    display = rows["mistral"]["reviewed_account"]["display_facts"]
    assert display[0] == {
        "kind": "requests_per_second",
        "value": 12.5,
        "period": "per_second",
        "precision": "exact",
    }
    assert display[1]["kind"] == "monthly_allowance_usd"
    assert display[2]["precision"] == "rounded"
    assert rows["openrouter"]["reviewed_account"]["qualification_basis"] == "public_conditions"
    for provider in ("sambanova", "huggingface", "cerebras"):
        row = rows[provider]
        assert row["free_eligible"] is False and row["enabled"] is False
        assert row["free_verified_at"] is row["free_evidence_expires_at"] is None
        assert row["provider_limits"] == [] and row["observations"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_field",
        "secret_note",
        "email",
        "source_query",
        "bad_kind",
        "renewed_review",
        "enable",
        "authoritative_derived",
        "extended_derived",
        "bad_billing",
        "invented_qualification",
    ],
)
def test_invalid_or_secret_bearing_metadata_fails_closed(tmp_path, mutation):
    from quota_broker.dashboard_reviewed import create_reviewed

    config, profile, targets = write_bundle(tmp_path)
    if mutation == "extra_field":
        profile["targets"][0]["api_key"] = "synthetic-secret"
    elif mutation == "secret_note":
        profile["targets"][0]["notes"] = ["Bearer synthetic-secret"]
    elif mutation == "email":
        profile["targets"][0]["notes"] = ["person@example.invalid"]
    elif mutation == "source_query":
        profile["targets"][0]["sources"] = ["https://build.nvidia.com/?api_key=secret"]
    elif mutation == "bad_kind":
        profile["kind"] = "live"
    elif mutation == "renewed_review":
        profile["review_due_at"] = (DUE + timedelta(days=1)).isoformat()
    elif mutation == "enable":
        targets[0]["enabled"] = True
    elif mutation == "authoritative_derived":
        targets[4]["provider_quota_facts"][0]["remaining"]["provenance"] = "official"
    elif mutation == "extended_derived":
        targets[4]["provider_quota_facts"][0]["remaining"]["valid_until"] = DUE.isoformat()
    elif mutation == "bad_billing":
        profile["targets"][0]["billing_state"] = "disabled"
    else:
        targets[-1]["free_eligible"] = True
        targets[-1]["verified_at"] = ANCHOR.isoformat()
        targets[-1]["expires_at"] = DUE.isoformat()
    config.write_text(json.dumps(profile))
    (config.parent / "gateway.json").write_text(json.dumps({"targets": targets}))
    with pytest.raises(ConfigError, match="invalid reviewed profile"):
        create_reviewed(tmp_path, config, clock=lambda: ANCHOR)


@pytest.mark.parametrize("which", ["directory", "metadata", "gateway", "registry", "symlink"])
def test_reviewed_bundle_requires_private_regular_files(tmp_path, which):
    from quota_broker.dashboard_reviewed import create_reviewed

    config, _profile, _targets = write_bundle(tmp_path)
    if which == "directory":
        config.parent.chmod(0o755)
    elif which == "symlink":
        original = config.parent / "original.json"
        config.rename(original)
        config.symlink_to(original)
    else:
        path = config if which == "metadata" else config.parent / (which + ".json")
        path.chmod(0o644)
    with pytest.raises(ConfigError, match="invalid reviewed profile"):
        create_reviewed(tmp_path, config, clock=lambda: ANCHOR)


@pytest.mark.parametrize("other", ["--demo", "--upstream"])
def test_reviewed_cli_mode_is_mutually_exclusive(other, tmp_path, monkeypatch):
    from quota_broker.cli import main

    args = [
        "quota-broker",
        "dashboard-serve",
        "--reviewed-config",
        str(tmp_path / "reviewed.json"),
        other,
    ]
    if other == "--upstream":
        args.append("http://127.0.0.1:9")
    args.extend(["--dashboard-token-file", str(tmp_path / "unused")])
    monkeypatch.setattr("sys.argv", args)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_frontend_labels_reviewed_evidence_units_and_unknown_billing(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for optional actual frontend rendering test")
    board, _source, _config, _profile, _targets = load_board(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(board.snapshot()))
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
class Element {
 constructor(){this.hidden=false;this.value='';this.children=[];this.handlers={};this.textContent='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 get firstChild(){return this.children[0];}
 addEventListener(name,cb){this.handlers[name]=cb;}
 setAttribute(){}
}
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const snapshot=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const context={document:{getElementById:get,createElement:()=>new Element(),createTextNode:text=>text},fetch:async()=>({ok:true,status:200,json:async()=>snapshot}),Intl,Date,crypto:{randomUUID:()=> 'synthetic'},console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const flatten=e=>typeof e==='string'?e:(e.textContent+' '+e.children.map(flatten).join(' '));
setImmediate(()=>{try{
 const text=flatten(get('providers'));
 for(const expected of ['計費狀態未核對','公開免費條件適用','供應商上限','12.5 requests/second','10 USD','不換算每分鐘','本機再核對期限','非點數到期','推算','非即時同步']) {
  assert.ok((text+' '+get('source').textContent).includes(expected),expected);
 }
 assert.ok(!text.includes('750'));
 const google=get('providers').children.find(row=>flatten(row).includes('google'));
 assert.ok(!flatten(google).includes('快照已過時'),'unknown remaining must not look expired');
}catch(error){console.error(error);process.exitCode=1;}});
"""
    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(ROOT / "src/quota_broker/dashboard_assets/dashboard.js"),
            str(snapshot),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_unknown_billing_does_not_become_paid_in_blocking_reasons(tmp_path):
    board, _source, _config, _profile, _targets = load_board(tmp_path)
    snapshot = board.snapshot()
    google = next(row for row in snapshot["targets"] if row["provider"] == "google")
    assert "billing_enabled" not in google["reasons"]
    assert "billing_state_unknown" in google["reasons"]
    result = board.advice(
        {
            "request_key": "billing-reasons",
            "capability": "text_generation",
            "input": "fixture",
            "max_output_tokens": 16,
        }
    )
    assert all("billing_enabled" not in row["reasons"] for row in result["candidates"])
