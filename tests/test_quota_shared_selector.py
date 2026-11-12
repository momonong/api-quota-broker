"""The two service accounts share code, while data and credentials stay separate."""

import ast
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dashboard_unit():
    tree = ast.parse((ROOT / "deploy/quota-web/gate.py").read_text())
    node = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DASHBOARD_UNIT"
            for target in node.targets
        )
    )
    return ast.literal_eval(node.value).decode()


def test_dashboard_uses_one_critical_code_selector():
    unit = dashboard_unit()
    command = next(
        line.removeprefix("ExecStart=")
        for line in unit.splitlines()
        if line.startswith("ExecStart=")
    )
    words = shlex.split(command)
    assert words[:6] == [
        "/opt/api-quota-broker/current/runtime/bin/python",
        "-I",
        "-B",
        "-m",
        "quota_broker.cli",
        "dashboard-serve",
    ]
    assert "/opt/api-quota-dashboard/current" not in command


def test_shared_code_does_not_share_accounts_data_or_secrets():
    unit = dashboard_unit()
    assert "User=api-quota-dashboard\n" in unit
    assert "Group=api-quota-dashboard\n" in unit
    assert "WorkingDirectory=/var/lib/api-quota-dashboard\n" in unit
    assert "ProtectSystem=strict\n" in unit
    assert "RuntimeDirectoryMode=0700\n" in unit
    assert (
        "LoadCredentialEncrypted=read_plan_token:/etc/api-quota-dashboard/credentials/read_plan_token.cred\n"
        in unit
    )
    assert "--activity-token-file /run/api-quota-dashboard/read_plan_token" in unit
    assert "--dashboard-password-file /etc/api-quota-dashboard/browser-auth.json" in unit
    assert "/etc/api-quota-broker" not in unit
    assert "/var/lib/api-quota-broker" not in unit
    assert "SupplementaryGroups=" not in unit
