"""Display evidence cannot weaken the existing conservative dispatch flags."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_dashboard import dashboard
from test_gateway import NOW, task

from quota_broker.config import ConfigError, load_gateway_config
from quota_broker.core import Broker


def config_document():
    return json.loads((Path(__file__).parents[1] / "gateway.example.json").read_text())


def test_explicit_unknown_is_not_a_paid_account_label_and_still_blocks_routing(tmp_path):
    board, source = dashboard(tmp_path)
    previous = source.gateway.targets[0]
    unknown = replace(
        previous, enabled=False, billing_enabled=True, billing_evidence_state="unknown"
    )
    source.gateway.targets = source.gateway.broker.targets = (unknown,)
    projected = board.snapshot()["targets"][0]
    assert projected["billing_state"] == "unknown"
    assert projected["billing_enabled"] is None
    assert projected["state"] == "blocked"
    assert "billing_enabled" not in projected["reasons"]
    assert "billing_state_unknown" in projected["reasons"]
    assert source.gateway.explain(task())["selected_target_id"] is None
    assert board.advice(task())["selected_target"] is None
    assert Broker._snapshot(unknown) == Broker._snapshot(previous)


def test_optional_billing_evidence_parser_and_catalog_backwards_compatibility(tmp_path):
    document = config_document()
    path = tmp_path / "gateway.json"
    for state in (None, "unknown", "disabled", "enabled"):
        if state is None:
            document["targets"][0].pop("billing_evidence_state", None)
        else:
            document["targets"][0]["billing_evidence_state"] = state
        path.write_text(json.dumps(document))
        target = load_gateway_config(path)[0]
        assert target.billing_evidence_state == state
        row = Broker(tmp_path / "catalog.sqlite", (target,), clock=lambda: NOW).catalog()[0]
        if state is None:
            assert "billing_evidence_state" not in row
        else:
            assert row["billing_evidence_state"] == state
        assert row["billing_enabled"] == document["targets"][0]["billing_enabled"]


@pytest.mark.parametrize("state", ["maybe", True, 0, [], {}])
def test_invalid_billing_evidence_is_rejected_before_any_runtime_initialization(tmp_path, state):
    document = config_document()
    document["targets"][0]["billing_evidence_state"] = state
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ConfigError, match="billing evidence"):
        load_gateway_config(path)
