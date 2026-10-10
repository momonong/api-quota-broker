"""Build a local immutable 1.0 review packet; no remote, credential or provider IO."""

import ast
import hashlib
import importlib.util
import io
import json
import os
import shlex
import sys
import tarfile
from pathlib import Path

REMOTE = "/var/tmp/api-quota-broker-normal-v1-review-2026-10-09-r2"
ROOT = Path(__file__).resolve().parents[2]
R2 = ROOT / "docs/evidence/asus-seven-pool/review-2026-10-08-r2"


def load(path, label):
    spec = importlib.util.spec_from_file_location(label, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[label] = module
    spec.loader.exec_module(module)
    return module


def wrapper(expected):
    # Reuse the reviewed two-phase parser/TTY/metadata protection, with exact
    # replacements of entry/unit literals. The sealed r2 bytes stay untouched.
    builder = load(ROOT / "deploy/asus/build_seven_pool_review.py", "normal_wrapper_basis")
    builder.REMOTE = REMOTE
    result = builder.wrapper(expected).decode()
    for old, new in (
        ("seven-pool-once.sh", "normal-pool-once.sh"),
        ("verify_seven_pool_offline.py", "verify_normal_pool_offline.py"),
        ("aqb-seven-pool-", "aqb-normal-pool-"),
        ("api-quota-broker-seven-audit", "api-quota-broker-normal-audit"),
        ("api-quota-broker-seven-pool", "api-quota-broker-normal-v1"),
        ('str(target/"seven_pool_operator.py")', 'str(target/"normal_pool_operator.py")'),
        ("seven_pool_private_readonly_gate", "normal_pool_private_readonly_gate"),
        ("seven_pool_review_plan", "normal_pool_review_plan"),
        ("seven_pool_outer_gate", "normal_pool_outer_gate"),
    ):
        result = result.replace(old, new)
    code = shlex.split(result.split(" -c ", 1)[1])[0]
    operator = load(ROOT / "deploy/asus/normal_pool_operator.py", "normal_diagnostic_contract")

    def replace_once(old, new):
        nonlocal code
        assert code.count(old) == 1, old
        code = code.replace(old, new)

    replace_once(
        'env={"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}',
        'env={"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"}\n'
        f"safe_stages={sorted(operator.SAFE_STAGES)!r}\nsafe_codes={sorted(operator.SAFE_CODES)!r}\n"
        'stage="host_gate"\nreturncode=None\ntarget=None\nroot_copy_completed=False',
    )
    replace_once("    tty=os.open(", '    stage="tty_gate"\n    tty=os.open(')
    replace_once(
        "    info=source.lstat();", '    stage="source_metadata"\n    info=source.lstat();'
    )
    replace_once(
        '    tmp=Path("/var/tmp").lstat();',
        '    stage="root_copy"\n    tmp=Path("/var/tmp").lstat();',
    )
    replace_once(
        "    common=(", '    root_copy_completed=True\n    stage="unit_absence"\n    common=('
    )
    replace_once("    audit=common+", '    stage="audit_invocation"\n    audit=common+')
    replace_once(
        '"--property=ProtectSystem=strict",',
        '"--property=ProtectSystem=strict","--property=TemporaryFileSystem=/tmp:rw,mode=0700,size=32M","--property=ReadWritePaths=/tmp",',
    )
    replace_once(
        "    need(p.returncode==0 and len(p.stdout)<=32768)\n    gate=json.loads(p.stdout);",
        """    returncode=p.returncode
    need(len(p.stdout)<=32768)
    stage="audit_response"
    gate=json.loads(p.stdout)
    if gate.get("status")=="blocked":
        child_stage=gate.get("stage")
        child_code=gate.get("code")
        child_rc=gate.get("returncode")
        result={"status":"blocked","stage":child_stage if child_stage in safe_stages else "readonly_audit",
                "code":child_code if child_code in safe_codes else "normal_pool_entry_gate",
                "returncode":p.returncode,"inner_returncode":child_rc if type(child_rc) is int and -128<=child_rc<=255 else None,
                "root_copy_completed":True,"root_copy_directory":str(target),
                "apply_started":False,"automatic_retry":False,"provider_posts":0}
        os.write(1,(json.dumps(result,sort_keys=True)+"\\n").encode())
        raise SystemExit(1)
    need(p.returncode==0)
    """,
    )
    replace_once(
        '    fd=os.open(target/"audit.json",',
        '    stage="audit_receipt"\n    fd=os.open(target/"audit.json",',
    )
    replace_once("    argv=common+", '    stage="apply_handoff"\n    argv=common+')
    start = code.index("except BaseException:\n")
    code = (
        code[:start]
        + """except SystemExit:
    raise
except BaseException as error:
    code=("normal_pool_outer_timeout" if isinstance(error,subprocess.TimeoutExpired)
          else "normal_audit_response_invalid" if isinstance(error,json.JSONDecodeError)
          else "normal_pool_outer_gate")
    result={"status":"blocked","stage":stage,"code":code,"returncode":returncode,
            "root_copy_completed":root_copy_completed,"root_copy_directory":str(target) if target else None,
            "apply_started":False,"automatic_retry":False,
            "provider_posts":None if stage=="apply_handoff" else 0,
            "provider_posts_status":"unknown" if stage=="apply_handoff" else "not_authorized_in_this_stage"}
    os.write(1,(json.dumps(result,sort_keys=True)+"\\n").encode())
    raise SystemExit(1)
"""
    )
    # Re-quote the entire root program, preserving literal newlines and payload hashes.
    result = result.split(" -c ", 1)[0] + " -c " + shlex.quote(code) + " </dev/tty\n"
    tree = ast.parse(code)
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)]
    pinned = ast.literal_eval(
        next(
            node.value
            for node in assignments
            if any(isinstance(t, ast.Name) and t.id == "expected" for t in node.targets)
        )
    )
    assert pinned == expected
    compile(code, "<normal-sealed-root>", "exec")
    assert any(isinstance(n, ast.Constant) and n.value == b"\n" for n in ast.walk(tree))
    return result.encode()


def build(destination, wheel_path):
    builder = load(ROOT / "scripts/build_asus_release.py", "normal_release_builder")
    operator = load(ROOT / "deploy/asus/normal_pool_operator.py", "normal_operator_builder")
    artifact = builder.build_release(ROOT)
    wheel = Path(wheel_path).read_bytes()
    operator.wheel_files(wheel, artifact.manifest["release"]["files"])
    r2_seal = json.loads((R2 / "seal.json").read_text())
    legacy = (
        "pool_base.py",
        "pool_base_plan.py",
        "seven_pool_operator.py",
        "seven_pool_plan.py",
        "ops_history_projection.py",
    )
    files = {name: (R2 / name).read_bytes() for name in legacy}
    for name in legacy:
        assert hashlib.sha256(files[name]).hexdigest() == r2_seal["files"][name]
    files.update(
        {
            "source.tar": artifact.archive,
            "project.whl": wheel,
            "build_asus_release.py": (ROOT / "scripts/build_asus_release.py").read_bytes(),
            "normal_pool_plan.py": (ROOT / "deploy/asus/normal_pool_plan.py").read_bytes(),
            "normal_pool_operator.py": (ROOT / "deploy/asus/normal_pool_operator.py").read_bytes(),
        }
    )
    for name in ("api-quota-broker.service", "api-quota-broker-client.service"):
        source_name = "api-quota-broker-v1.service" if name == "api-quota-broker.service" else name
        files[name] = (ROOT / "deploy/asus" / source_name).read_bytes()
        row = next(
            r
            for r in artifact.manifest["release"]["files"]
            if r["path"] == "deploy/asus/" + source_name
        )
        assert hashlib.sha256(files[name]).hexdigest() == row["sha256"]
    policy = {
        "schema": 1,
        "source_archive_sha256": hashlib.sha256(artifact.archive).hexdigest(),
        "payload_manifest_sha256": artifact.manifest["payload_manifest_sha256"],
        "project_wheel_sha256": hashlib.sha256(wheel).hexdigest(),
        "normal_plan_sha256": hashlib.sha256(files["normal_pool_plan.py"]).hexdigest(),
        "baseline_release": operator.OLD_RELEASE,
        "baseline_config_sha256": operator.OLD_CONFIG_SHA,
        "baseline_unit_sha256": operator.OLD_UNIT_SHA,
        "baseline_entry_sha256": operator.OLD_ENTRY_SHA,
        "provider_posts_max": 3,
        "max_live_output_tokens": 2048,
        "default_text_output_tokens": 1024,
        "normal_admission_basis": "preserve existing r2 config evidence, expiry, IDs, buckets and holds",
        "fixture_only_admission": json.loads((R2 / "policy.json").read_text())["admission"],
        "new_token_creation": 0,
        "provider_metadata_GET": 0,
    }
    files["policy.json"] = (json.dumps(policy, sort_keys=True, indent=2) + "\n").encode()
    seal = {
        "schema": 1,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }
    files["seal.json"] = (json.dumps(seal, sort_keys=True, indent=2) + "\n").encode()
    files["normal-pool-once.sh"] = wrapper(
        {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    )
    files["verify_normal_pool_offline.py"] = (
        ROOT / "deploy/asus/verify_normal_pool_offline.py"
    ).read_bytes()
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    for name, raw in files.items():
        with (destination / name).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    outer = destination.with_suffix(".tar")
    with outer.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(out.getvalue())
    return {
        "status": "sealed",
        "review_directory": str(destination),
        "remote_directory": REMOTE,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "outer_path": str(outer),
        "outer_sha256": hashlib.sha256(out.getvalue()).hexdigest(),
        "release": artifact.manifest,
        "root_deployed": False,
        "uploaded": False,
        "provider_posts": 0,
        "credential_reads": 0,
        "service_changes": 0,
    }


if __name__ == "__main__":
    print('{"mode":"normal_pool_local_review_builder","provider_posts":0,"host_changes":0}')
