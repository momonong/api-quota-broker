"""Actual readonly failure/private tmpfs fix and shell child-code preservation."""

import importlib.util
import json
import shlex
import subprocess
import sys

import pytest


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load("deploy/asus/build_normal_pool_review.py", "audit_repair_builder")
operator = load("deploy/asus/normal_pool_operator.py", "audit_repair_operator")


def root_code():
    raw = builder.wrapper({"public-fixture.txt": "0" * 64}).decode()
    return shlex.split(raw.split(" -c ", 1)[1])[0]


def test_fixed_sandbox_keeps_claim_budget_and_private_data_contract():
    code = root_code()
    compile(code, "fixed-wrapper", "exec")
    assert "TemporaryFileSystem=/tmp:rw,mode=0700,size=32M" in code
    assert "ReadWritePaths=/tmp" in code and "PrivateTmp=yes" not in code
    assert "ReadOnlyPaths=/var/lib/api-quota-broker /var/backups/api-quota-broker" in code
    assert "InaccessiblePaths=/etc/api-quota-broker/credentials" in code
    assert operator.CLAIM.name == "normal-pool-v1-2026-10-08-r1.claim.json"
    assert operator.JOURNAL.name == "normal-pool-v1-2026-10-08-r1.json"
    assert operator.PREFIX == "asus-normal-v1-2026-10-08-r1-"


@pytest.mark.parametrize(
    "stage,posts",
    [("unit_verify", 0), ("history_projection", 0), ("queue_gate", 0), ("apply_transaction", None)],
)
def test_classified_errors_and_unknown_apply_counts_survive_projection(stage, posts):
    known = operator.failure_projection(
        operator.Blocked("native_unit_verify_failed"), stage=stage, returncode=1
    )
    assert known["code"] == "native_unit_verify_failed" and known["stage"] == stage
    assert known["returncode"] == 1 and known["provider_posts"] == posts
    hidden = operator.failure_projection(
        RuntimeError("secret/raw error must not cross"), stage=stage
    )
    assert "secret" not in json.dumps(hidden) and hidden["code"] == "normal_pool_entry_gate"


MOUNT_REPRO = r"""
import os,json,subprocess,tempfile,shutil
from pathlib import Path
root=Path(tempfile.mkdtemp(prefix='aqb-public-audit-repair-',dir='/var/tmp'))
unit=root/'public-fixture.service';unit.write_text('[Unit]\nDescription=Public fixture\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n')
protected=root/'protected';protected.mkdir();(protected/'marker').write_text('public fixture')
try:
 for path in ('/tmp','/var/tmp'):
  subprocess.run(['/usr/bin/mount','--bind',path,path],check=True,capture_output=True)
  subprocess.run(['/usr/bin/mount','-o','remount,bind,ro',path],check=True,capture_output=True)
 def verify():return subprocess.run(['/usr/bin/systemd-analyze','verify',str(unit)],env={'PATH':'/usr/bin:/bin','LANG':'C'},capture_output=True,check=False,timeout=20)
 broken=verify();assert broken.returncode==1 and b'Read-only file system' in broken.stderr
 subprocess.run(['/usr/bin/mount','-t','tmpfs','-o','mode=0700,size=32M','tmpfs','/tmp'],check=True,capture_output=True)
 fixed=verify();assert fixed.returncode==0
 assert unit.exists() and os.statvfs(protected).f_flag&os.ST_RDONLY
 try:(protected/'bad').write_text('public fixture')
 except OSError as error:assert error.errno==30
 else:raise AssertionError('readonly data writable')
 print(json.dumps({'broken_rc':1,'fixed_rc':0,'payload_visible':True,'protected_write_denied':True,'production_service_changes':0}))
finally:
 subprocess.run(['/usr/bin/mount','-o','remount,bind,rw','/var/tmp'],check=True,capture_output=True)
 shutil.rmtree(root)
"""


def test_actual_mount_namespace_tmpfs_correction():
    if sys.platform != "linux":
        pytest.skip("Linux mount namespace proof only")
    supported = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "/usr/bin/true"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if supported.returncode:
        pytest.skip("user/mount namespace unavailable; native sandbox proof remains required")
    # Literal program text is passed directly, never via an interpolated shell.
    code = MOUNT_REPRO
    result = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", sys.executable, "-c", code],
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["fixed_rc"] == 0


@pytest.mark.parametrize(
    "fault,expected_code",
    [
        ("nonjson", "normal_audit_response_invalid"),
        ("timeout", "normal_pool_outer_timeout"),
        ("oserror", "normal_pool_outer_gate"),
    ],
)
def test_actual_outer_except_classifies_without_raw_child_or_exception(
    tmp_path, fault, expected_code
):
    code = root_code()
    response_start = code.index("    returncode=p.returncode\n")
    response_end = code.index('    fd=os.open(target/"audit.json"')
    response = code[response_start:response_end]
    catches = code[code.index("except SystemExit:\n") :]
    child = tmp_path / "child.py"
    child.write_text(
        "import time\ntime.sleep(0.3)\n"
        if fault == "timeout"
        else "print('RAW_PRIVATE_PUBLIC_FIXTURE_NOT_FOR_OUTPUT')\nraise SystemExit(1)\n"
    )
    argv = (
        [sys.executable, str(child)]
        if fault != "oserror"
        else ["/definitely-missing-public-fixture"]
    )
    setup = (
        "import json,os,subprocess\nfrom pathlib import Path\n"
        + f"safe_stages={sorted(operator.SAFE_STAGES)!r}\nsafe_codes={sorted(operator.SAFE_CODES)!r}\n"
        + "target=Path('/var/tmp/aqb-public-fixture')\nroot_copy_completed=True\nstage='audit_invocation'\nreturncode=None\ndef need(v):\n if not v:raise ValueError('fixed_gate')\ntry:\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        setup
        + f"    p=subprocess.run({argv!r},capture_output=True,timeout=0.03,check=False)\n"
        + response
        + catches
    )
    shell = tmp_path / "shell.sh"
    shell.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(parent)) + "\n"
    )
    result = subprocess.run(["/bin/sh", str(shell)], capture_output=True, timeout=10, check=False)
    assert result.returncode == 1, result.stderr.decode()
    value = json.loads(result.stdout)
    assert value["code"] == expected_code
    assert "RAW_PRIVATE" not in json.dumps(value) and value["apply_started"] is False


@pytest.mark.parametrize(
    "child_code,child_stage,inner_rc",
    [
        ("native_unit_verify_failed", "unit_verify", 1),
        ("claim_collision", "readonly_audit", None),
        ("private_metadata_untrusted", "history_projection", None),
    ],
)
def test_real_shell_to_python_child_failure_json_chain(tmp_path, child_code, child_stage, inner_rc):
    # Execute the actual generated catch/response segment with a real shell and
    # Python child. No root program, systemd unit, DB or credential is executed.
    code = root_code()
    start = code.index("    returncode=p.returncode\n")
    end = code.index('    fd=os.open(target/"audit.json"')
    segment = code[start:end]
    failure = {
        "status": "blocked",
        "code": child_code,
        "stage": child_stage,
        "returncode": inner_rc,
    }
    child = tmp_path / "child.py"
    child.write_text("import json\nprint(" + repr(json.dumps(failure)) + ")\nraise SystemExit(1)\n")
    launcher = tmp_path / "parent.py"
    setup = (
        "import json,os,subprocess\nfrom pathlib import Path\n"
        + f"safe_stages={sorted(operator.SAFE_STAGES)!r}\nsafe_codes={sorted(operator.SAFE_CODES)!r}\n"
        + f'target=Path("/var/tmp/aqb-public-fixture")\np=subprocess.run([{sys.executable!r},{str(child)!r}],capture_output=True,check=False)\n'
        + "def need(v):\n if not v:raise ValueError('fixed_gate')\ntry:\n"
    )
    launcher.write_text(setup + segment + "except SystemExit:\n raise\n")
    shell = tmp_path / "launch.sh"
    shell.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(launcher)) + "\n"
    )
    result = subprocess.run(["/bin/sh", str(shell)], capture_output=True, timeout=10, check=False)
    assert result.returncode == 1, result.stderr.decode()
    payload = json.loads(result.stdout)
    assert payload["code"] == child_code and payload["stage"] == child_stage
    assert payload["returncode"] == 1 and payload["inner_returncode"] == inner_rc
    assert payload["apply_started"] is False and payload["provider_posts"] == 0


def test_operator_main_preserves_known_sandbox_failure(monkeypatch, capsys):
    monkeypatch.setattr(operator.sys, "argv", ["normal_pool_operator.py", "--audit"])
    # A real package metadata mismatch is reported before any private read.
    assert operator.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "package_untrusted" and payload["stage"] == "package_metadata"
