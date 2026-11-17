"""The two service accounts share code, while data and credentials stay separate."""

import ast
import shlex
import subprocess
import sys
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


def test_gate_import_is_stdlib_only_and_has_no_host_or_credential_access():
    script = r"""
import ast, importlib, pathlib, sys
path = pathlib.Path(sys.argv[1])
raw = path.read_bytes()
tree = ast.parse(raw)
for node in tree.body:
    if isinstance(node, ast.Import):
        names = [item.name for item in node.names]
    elif isinstance(node, ast.ImportFrom):
        names = [node.module]
    else:
        continue
    for name in names:
        assert name.split('.')[0] in sys.stdlib_module_names
        importlib.import_module(name)
def audit(event, args):
    if event == 'open' or event.startswith(('socket.', 'subprocess.', 'os.', 'resource.')):
        raise AssertionError('gate import attempted host access: ' + event)
sys.addaudithook(audit)
namespace = {'__name__': 'quota_gate_import_fixture', '__file__': str(path)}
exec(compile(raw, str(path), 'exec'), namespace)
assert callable(namespace['apply_buffer'])
assert callable(namespace['main'])
print('gate-import-no-host-access')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(ROOT / "deploy/quota-web/gate.py")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "gate-import-no-host-access"
