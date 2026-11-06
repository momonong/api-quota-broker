"""Credit accounts remain evidence, never executable routing capacity."""

import copy
import json
from datetime import timedelta

import pytest
from test_dashboard_reviewed import ANCHOR, load_board

from quota_broker.config import ConfigError
from quota_broker.dashboard import UpstreamSource


def account_document():
    return {
        "schema_version": 1,
        "accounts": [
            {
                "provider": "openai",
                "qualification": "linked_chatgpt_subscription_benefit",
                "billing": "not_started_no_payment_method",
                "currency": "USD",
                "balance": "23.45",
                "grants": [
                    {
                        "amount": "23.45",
                        "remaining": "23.45",
                        "received": {"value": "2026-01-03", "precision": "date"},
                        "expires": {"value": "2026-01-08", "precision": "date"},
                        "state": "available",
                    }
                ],
                "benefit": {
                    "recurrence": "conditional_on_eligible_subscription_renewal",
                    "renewal_amount": None,
                    "renews_at": None,
                    "rollover": False,
                },
                "model_limits": [
                    {
                        "model": "example-model",
                        "requests_per_minute": 10,
                        "tokens_per_minute": 1000,
                        "images_per_minute": None,
                        "batch_queue_tokens_per_day": 9000,
                    }
                ],
                "evidence": {
                    "provenance": "observed",
                    "as_of": ANCHOR.isoformat(),
                    "valid_until": (ANCHOR + timedelta(minutes=15)).isoformat(),
                    "time_precision": "account_pages_checked_during_same_audit_session",
                    "sources": [
                        "https://platform.openai.com/settings/organization/billing/credit-grants"
                    ],
                },
                "usage_page": {
                    "state": "no_data",
                    "from_date": "2025-12-28",
                    "through_date": "2026-01-03",
                    "scope": "all_api_sources_projects_keys",
                },
                "notes": ["Synthetic credit observation"],
            }
        ],
    }


def write_accounts(tmp_path, doc=None):
    directory = tmp_path / "accounts"
    directory.mkdir(mode=0o700)
    path = directory / "accounts.json"
    path.write_text(json.dumps(doc or account_document()))
    path.chmod(0o600)
    return path


def test_credits_precision_expiry_unknown_renewal_and_no_routing(tmp_path):
    from quota_broker.dashboard_accounts import CreditAccounts

    path = write_accounts(tmp_path)
    board, source, *_ = load_board(tmp_path)
    board.accounts = CreditAccounts(path)
    snapshot = board.snapshot()
    account = snapshot["accounts"][0]
    assert account["balance"] == "23.45" and account["current_balance"] == "23.45"
    assert account["grants"][0]["expires"] == {"value": "2026-01-08", "precision": "date"}
    assert account["benefit"]["renews_at"] is account["benefit"]["renewal_amount"] is None
    assert account["model_limits"][0]["batch_queue_tokens_per_day"] == 9000
    assert "remaining" not in account["model_limits"][0]
    assert len(snapshot["targets"]) == 10
    assert snapshot["activity_source"]["state"] == "unavailable"
    assert snapshot["usage"] is None and snapshot["recent"] is None
    result = board.advice(
        {
            "request_key": "credit-only",
            "capability": "text_generation",
            "input": "example",
            "max_output_tokens": 16,
        }
    )
    assert result["decision"] == "ask_user" and result["execution_allowed"] is False
    board.clock = lambda: ANCHOR + timedelta(minutes=16)
    source.gateway.clock = board.clock
    stale = board.snapshot()["accounts"][0]
    assert stale["balance"] == "23.45" and stale["current_balance"] is None
    assert not stale["evidence"]["current"]
    assert CreditAccounts(path).snapshot(board.clock())[0] == stale


@pytest.mark.parametrize(
    "mutation",
    [
        "secret",
        "secret_model",
        "url",
        "extra",
        "nan",
        "amount",
        "expiry",
        "long_validity",
        "permission",
        "duplicate",
    ],
)
def test_accounts_reject_unsafe_metadata(tmp_path, mutation):
    from quota_broker.dashboard_accounts import CreditAccounts

    doc = copy.deepcopy(account_document())
    account = doc["accounts"][0]
    if mutation == "secret":
        account["notes"] = ["Bearer synthetic-private-value"]
    elif mutation == "secret_model":
        account["model_limits"][0]["model"] = "sk-synthetic-credential-value"
    elif mutation == "url":
        account["evidence"]["sources"] = [
            "https://platform.openai.com/settings/organization/billing/credit-grants?token=example"
        ]
    elif mutation == "extra":
        account["api_key"] = "synthetic"
    elif mutation == "nan":
        account["balance"] = float("nan")
    elif mutation == "amount":
        account["balance"] = "23.4512345"
    elif mutation == "expiry":
        account["grants"][0]["expires"]["value"] = "2026-01-08T00:00:00Z"
    elif mutation == "long_validity":
        account["evidence"]["valid_until"] = (ANCHOR + timedelta(days=14)).isoformat()
    path = write_accounts(tmp_path, doc)
    if mutation == "permission":
        path.chmod(0o644)
    elif mutation == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1,"accounts":[]}')
    with pytest.raises(ConfigError, match="invalid credit accounts"):
        CreditAccounts(path)


def test_activity_only_has_no_explain_or_execution_route():
    from quota_broker.dashboard import ActivitySource, DashboardError

    source = ActivitySource("http://127.0.0.1:9", "synthetic-activity-only-token-0000000000")
    assert source.ROUTES == {
        ("GET", "/v1/catalog"),
        ("GET", "/v1/usage"),
        ("GET", "/v1/tasks?limit=6"),
    }
    with pytest.raises(DashboardError, match="route_not_allowed"):
        source._get("POST", "/v1/routes/explain", {})


def test_activity_failure_is_unknown_and_partial_rows_are_not_zero(tmp_path):
    board, *_ = load_board(tmp_path)
    activity = UpstreamSource("http://127.0.0.1:9", "synthetic-activity-only-token-0000000000")
    board.activity = activity
    snapshot = board.snapshot()
    assert snapshot["activity_source"]["state"] == "unavailable"
    assert snapshot["usage"] is None and snapshot["recent"] is None
    activity.usage = list
    activity.recent = lambda: {"tasks": []}
    snapshot = board.snapshot()
    assert snapshot["activity_source"]["state"] == "available"
    assert snapshot["usage"] == [] and snapshot["recent"] == []
    activity.usage = lambda: [{"provider": "openai"}]
    assert board.snapshot()["activity_source"]["state"] == "unavailable"


def test_frontend_credit_dates_unknown_usage_and_prefix_safe_fetch(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    from quota_broker.dashboard_accounts import CreditAccounts

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for DOM rendering check")
    board, *_ = load_board(tmp_path)
    board.accounts = CreditAccounts(write_accounts(tmp_path))
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(board.snapshot()))
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map(),paths=[];
class Element{constructor(){this.hidden=false;this.value='';this.children=[];this.textContent='';}append(...xs){this.children.push(...xs);}replaceChildren(...xs){this.children=xs;}get firstChild(){return this.children[0];}addEventListener(){}setAttribute(){}}
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const snapshot=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const context={document:{getElementById:get,createElement:()=>new Element(),createTextNode:x=>x},fetch:async path=>{paths.push(path);return {ok:true,json:async()=>snapshot};},Intl,Date,console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const flatten=e=>typeof e==='string'?e:(e.textContent+' '+e.children.map(flatten).join(' '));
setImmediate(()=>{try{
const text=flatten(get('accounts'));
for(const value of ['23.45 USD','2026-01-08','時間與時區未知','下次訂閱補充','未提供','Batch queue','無用量資料'])assert.ok(text.includes(value),value);
assert.ok(!text.includes('00:00:00'),'date-only must not acquire a timestamp');
assert.ok(flatten(get('usage')).includes('用量來源未接入'));
assert.equal(paths[0],'./api/snapshot');
}catch(e){console.error(e);process.exitCode=1;}});
"""
    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(
                Path(__file__).resolve().parents[1]
                / "src/quota_broker/dashboard_assets/dashboard.js"
            ),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--activity-upstream", "http://127.0.0.1:9"],
        ["--activity-token-file", "/private/unused"],
        ["--activity-upstream", "http://127.0.0.1:9", "--activity-token-file", "/private/unused"],
    ],
)
def test_activity_cli_requires_pair_and_reviewed_mode(monkeypatch, args):
    from quota_broker.cli import main

    monkeypatch.setattr(
        "sys.argv",
        ["quota-broker", "dashboard-serve", *args, "--dashboard-token-file", "/private/unused"],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_activity_partial_recent_stays_unknown(tmp_path):
    board, *_ = load_board(tmp_path)
    activity = UpstreamSource("http://127.0.0.1:9", "synthetic-activity-only-token-0000000000")
    board.activity = activity
    activity.usage = list
    activity.recent = lambda: {"tasks": [{"state": "unknown"}]}
    snapshot = board.snapshot()
    assert snapshot["activity_source"]["state"] == "unavailable"
    assert snapshot["usage"] is snapshot["recent"] is None
