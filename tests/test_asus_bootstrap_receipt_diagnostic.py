import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "diagnostic", ROOT / "deploy/asus/bootstrap_receipt_diagnostic.py"
)
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)
SNAPSHOT = {
    "kind": "readonly_snapshot",
    "presence": dict.fromkeys(["claim", "result", "backup", "maintenance"], "absent"),
    "receipt": None,
}


def test_generated_diagnostic_only_invokes_fixed_reader():
    command = shlex.split(d.argv()[-1])
    compile(command[-1], "remote_diagnostic", "exec")
    assert command[-1].endswith("query_original_receipt()\n")
    compile(d.READER, "root_reader", "exec")
    assert "subprocess" not in d.READER and "O_WRONLY" not in d.READER


def test_real_pipe_diagnostic_fetches_once_after_ready(monkeypatch):
    result = {
        "kind": "diagnostic_result",
        "state": "received",
        "authentication_submissions": 1,
        "snapshot": SNAPSHOT,
    }
    code = f'import sys\nprint({json.dumps(d.t.READY)!r},flush=True)\nassert sys.stdin.buffer.readline()==b"fixture\\n"\nprint({json.dumps(result)!r},flush=True)'
    monkeypatch.setattr(d, "argv", lambda: [sys.executable, "-I", "-B", "-S", "-c", code])
    fetched = []

    def fetch():
        fetched.append(1)
        return bytearray(b"fixture")

    answer = d.query(fetch)
    assert fetched == [1] and answer["snapshot"] == SNAPSHOT


@pytest.mark.parametrize(
    "bad",
    [{"extra": "fixture-secret"}, {"receipt": {"status": "fixture-secret", "stage": "complete"}}],
)
def test_snapshot_never_reflects_unknown_fields_or_strings(bad):
    with pytest.raises(d.t.Denied):
        d.validate_snapshot({**SNAPSHOT, **bad})


def test_reader_absence_and_existing_receipt_fixture(tmp_path):
    # Ordinary-UID fixture exercises actual filesystem reads and JSON projection.
    code = d.READER.replace(
        "base=Path('/var/lib/api-quota-broker-ops')", f"base=Path({str(tmp_path)!r})"
    )
    code = code.replace(
        "assert os.geteuid()==0 and os.uname().nodename=='asus-ubuntu2604-server'",
        "assert os.geteuid()!=0",
    )
    code = code.replace("meta.st_uid==0", "meta.st_uid==os.geteuid()")

    def run():
        p = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert p.returncode == 0
        return json.loads(p.stdout)

    assert run() == SNAPSHOT
    path = tmp_path / "maintenance-bootstrap-r1.result.json"
    path.write_text(
        json.dumps(
            {
                "status": "blocked",
                "stage": "verify",
                "rollback_verified": True,
                "ignored": "fixture-secret",
            }
        )
    )
    path.chmod(0o600)
    result = run()
    assert result["presence"]["result"] == "present"
    assert result["receipt"] == {"status": "blocked", "stage": "verify", "rollback_verified": True}
    assert "fixture-secret" not in json.dumps(result)
    assert json.loads(path.read_text())["ignored"] == "fixture-secret"
