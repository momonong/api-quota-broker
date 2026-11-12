"""Private reviewed display evidence cannot alter the live execution authority."""

import copy
import json
import sqlite3

import pytest
from test_dashboard_reviewed import ANCHOR, write_bundle
from test_gateway import NOW
from test_shared_credit_admission import body, setup_credit

from quota_broker.dashboard import Dashboard, UpstreamSource
from quota_broker.dashboard_reviewed import create_reviewed


def reviewed_fixture(tmp_path):
    config, profile, targets = write_bundle(tmp_path)
    hf_id = next(row["id"] for row in targets if row["provider"] == "huggingface")
    row = next(row for row in profile["targets"] if row["target_id"] == hf_id)
    row["sources"] = ["https://huggingface.co/settings/billing"]
    row["display_facts"] = [
        {"kind": "credits_usd", "value": 0, "period": "account", "precision": "exact"}
    ]
    config.write_text(json.dumps(profile))
    return create_reviewed(tmp_path, config, clock=lambda: NOW), config


def live_fixture(tmp_path, reviewed):
    state = tmp_path / "live"
    state.mkdir(mode=0o700)
    gateway, _target, _calls, _clock, _access = setup_credit(state, unknown=True)
    gateway.run(body("preserved-unknown"))
    catalog = copy.deepcopy(reviewed.gateway.catalog())
    diagnostics = copy.deepcopy(reviewed.gateway.diagnostics()["targets"])
    runtime_catalog = gateway.catalog()[0]
    runtime_diag = gateway.diagnostics()["targets"][0]
    # Two live shared routes. Their flags/diagnostics remain authoritative.
    for provider, target_id, model in [
        ("openai", "openai", "gpt-6-luna"),
        ("openrouter", "live-openrouter", "liquid/lfm-2.5-2.6b:free"),
    ]:
        catalog.append(
            {**runtime_catalog, "target_id": target_id, "provider": provider, "model": model}
        )
        diagnostics.append(
            {**runtime_diag, "target_id": target_id, "provider": provider, "model": model}
        )
    source = UpstreamSource(
        "http://127.0.0.1:18587", "synthetic-read-plan-token-thirty-two-characters"
    )
    source.catalog = lambda: copy.deepcopy(catalog)
    source.diagnostics = lambda: {"as_of": NOW.isoformat(), "targets": copy.deepcopy(diagnostics)}
    source.usage = gateway.usage
    source.recent = lambda: {"tasks": []}
    source.models = lambda: {"models": []}
    def explain(task):
        plan = copy.deepcopy(gateway.explain(task))
        base = plan["candidates"][0]
        plan["candidates"] = [
            {
                **copy.deepcopy(base),
                "target_id": row["target_id"],
                "provider": row["provider"],
                "model": row["model"],
                "eligible": False,
                "reasons": ["disabled"] if not row["enabled"] else list(base["reasons"]),
            }
            for row in catalog
        ]
        return plan

    source.explain = explain
    return source, gateway, catalog, diagnostics


def test_upstream_displays_reviewed_zero_and_monthly_facts_without_changing_live_authority(
    tmp_path,
):
    reviewed, _config = reviewed_fixture(tmp_path)
    source, gateway, _catalog, _diag = live_fixture(tmp_path, reviewed)
    baseline = Dashboard(source, clock=lambda: NOW)
    before = baseline.snapshot()
    advice_before = baseline.advice(body("plan"))
    route_before = gateway.explain(body("plan"))
    with sqlite3.connect(gateway.db) as con:
        ledger_before = list(con.iterdump())
    board = Dashboard(source, clock=lambda: NOW, reviewed_evidence=reviewed)
    after = board.snapshot()
    before_targets = {row["target_id"]: row for row in before["targets"]}
    for row in after["targets"]:
        original = before_targets[row["target_id"]]
        for key in (
            "enabled",
            "free_eligible",
            "billing_enabled",
            "billing_state",
            "state",
            "credentials",
            "observations",
            "reasons",
            "required_capacity",
            "local_caps",
        ):
            assert row[key] == original[key], (row["target_id"], key)
    by_provider = {row["provider"]: row for row in after["targets"]}
    hf = by_provider["huggingface"]["reviewed_account"]
    assert hf["display_facts"][0]["value"] == 0
    assert hf["audit_session_at"] == ANCHOR.isoformat()
    mistral = by_provider["mistral"]["reviewed_account"]
    assert any(
        row["kind"] == "monthly_allowance_usd" and row["value"] == 10
        for row in mistral["display_facts"]
    )
    usage = board.usage_report({})["provider_usage"]["providers"]
    mistral_usage = next(row for row in usage if row["provider"] == "mistral")
    assert mistral_usage["as_of"] == ANCHOR.isoformat()
    assert all(row["as_of"] == ANCHOR.isoformat() for row in mistral_usage["metrics"])
    assert board.advice(body("plan")) == advice_before
    assert gateway.explain(body("plan")) == route_before
    with sqlite3.connect(gateway.db) as con:
        assert list(con.iterdump()) == ledger_before
    assert gateway.status("preserved-unknown")["state"] == "unknown"


@pytest.mark.parametrize(
    "ambiguity", ["wrong_model", "multiple_live_accounts", "multiple_reviewed_accounts"]
)
def test_overlay_requires_a_unique_exact_model_match_on_both_sides(tmp_path, ambiguity):
    reviewed, _config = reviewed_fixture(tmp_path)
    source, _gateway, catalog, diagnostics = live_fixture(tmp_path, reviewed)
    hf = next(row for row in catalog if row["provider"] == "huggingface")
    if ambiguity == "wrong_model":
        hf["model"] = "different-model"
        next(row for row in diagnostics if row["target_id"] == hf["target_id"])["model"] = hf[
            "model"
        ]
    elif ambiguity == "multiple_live_accounts":
        catalog.append({**hf, "target_id": "different-hf-account"})
        original = next(row for row in diagnostics if row["target_id"] == hf["target_id"])
        diagnostics.append({**original, "target_id": "different-hf-account"})
    else:
        original_catalog = reviewed.catalog

        def duplicated():
            rows = original_catalog()
            original = next(row for row in rows if row["provider"] == "huggingface")
            return rows + [{**original, "target_id": "second-reviewed-account"}]

        reviewed.catalog = duplicated
    board = Dashboard(source, clock=lambda: NOW, reviewed_evidence=reviewed)
    hf_rows = [row for row in board.snapshot()["targets"] if row["provider"] == "huggingface"]
    assert hf_rows and all(row["reviewed_account"] is None for row in hf_rows)


def test_cli_optional_overlay_loads_same_private_bundle_and_invalid_present_fails_closed(tmp_path):
    from quota_broker.cli import _upstream_reviewed_evidence

    _reviewed, config = reviewed_fixture(tmp_path)
    accounts = config.parent / "accounts.json"
    provider = config.parent / "provider-observations.json"
    accounts.touch(mode=0o600)
    provider.touch(mode=0o600)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    loaded = _upstream_reviewed_evidence(runtime, str(accounts), str(provider))
    assert loaded is not None
    assert loaded.source_metadata()["audit_session_at"] == ANCHOR.isoformat()
    config.write_text("{}")
    with pytest.raises(ValueError):
        _upstream_reviewed_evidence(runtime, str(accounts), str(provider))
    config.unlink()
    assert _upstream_reviewed_evidence(runtime, str(accounts), str(provider)) is None
    config.symlink_to(config.parent / "missing-review.json")
    with pytest.raises(ValueError):
        _upstream_reviewed_evidence(runtime, str(accounts), str(provider))


def test_equal_grant_values_and_no_data_never_become_zero_actual_cost_after_a_call(tmp_path):
    from test_dashboard_accounts import account_document, write_accounts

    from quota_broker.dashboard_accounts import CreditAccounts

    reviewed, _config = reviewed_fixture(tmp_path)
    source, gateway, _catalog, _diag = live_fixture(tmp_path, reviewed)
    document = account_document()
    account = document["accounts"][0]
    account["balance"] = "200.00"
    account["grants"][0]["amount"] = account["grants"][0]["remaining"] = "200.00"
    board = Dashboard(
        source,
        clock=lambda: NOW,
        reviewed_evidence=reviewed,
        accounts=CreditAccounts(write_accounts(tmp_path, document)),
    )
    with sqlite3.connect(gateway.db) as con:
        before = list(con.iterdump())
    row = next(
        row
        for row in board.usage_report({})["provider_usage"]["providers"]
        if row["provider"] == "openai"
    )
    grant = next(metric for metric in row["metrics"] if metric["metric"] == "credit_grant_spend")
    no_data = next(metric for metric in row["metrics"] if metric["metric"] == "usage_page_audit")
    assert grant["used"] is None and grant["limit"] == grant["remaining"] == "200.00"
    assert grant["label"] == "帳戶點數觀測，非實際支出"
    assert no_data["used"] is None and no_data["reported_state"] == "no_data"
    assert "未回報用量資料" in no_data["label"]
    assert grant["as_of"] == account["evidence"]["as_of"]
    assert no_data["as_of"] == account["evidence"]["as_of"]
    assert gateway.status("preserved-unknown")["state"] == "unknown"
    with sqlite3.connect(gateway.db) as con:
        assert list(con.iterdump()) == before


def test_cli_upstream_passes_the_sibling_reviewed_source_to_the_dashboard(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quota_broker.cli import main

    reviewed, config = reviewed_fixture(tmp_path)
    source, _gateway, _catalog, _diag = live_fixture(tmp_path, reviewed)
    from test_dashboard_accounts import account_document

    for name in ("accounts.json", "provider-observations.json"):
        path = config.parent / name
        path.write_text(json.dumps(account_document()) if name == "accounts.json" else "{}")
        path.chmod(0o600)
    token = tmp_path / "read-token"
    token.write_text("synthetic-scoped-read-plan-token-thirty-two")
    token.chmod(0o600)
    captured = []
    monkeypatch.setattr("quota_broker.dashboard.UpstreamSource", lambda *_args: source)

    def server(board, *_args, **_kwargs):
        assert board.source is source and board.source.kind == "gateway"
        assert board.reviewed_evidence.kind == "reviewed"
        targets = board._display_targets(
            source.catalog(), {row["target_id"]: row for row in source.diagnostics()["targets"]}
        )
        hf = next(row for row in targets if row["provider"] == "huggingface")
        captured.append(hf["reviewed_account"]["display_facts"][0]["value"])
        return SimpleNamespace(
            server_port=18586, serve_forever=lambda: None, server_close=lambda: None
        )

    monkeypatch.setattr("quota_broker.dashboard_server.make_dashboard_server", server)
    monkeypatch.setattr(
        "sys.argv",
        [
            "quota-broker",
            "dashboard-serve",
            "--upstream",
            "http://127.0.0.1:18587",
            "--upstream-token-file",
            str(token),
            "--accounts-file",
            str(config.parent / "accounts.json"),
            "--provider-metadata-file",
            str(config.parent / "provider-observations.json"),
            "--dashboard-password-file",
            "/private/browser-auth-is-not-read-by-test",
        ],
    )
    main()
    assert captured == [0]
