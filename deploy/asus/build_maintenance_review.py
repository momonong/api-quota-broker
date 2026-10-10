"""Build the reviewed root capability once; application archive stays untrusted data."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deploy/asus"
NORMAL = ROOT / "docs/evidence/asus-normal-v1/review-2026-10-09-r2"
DEST = ROOT / "docs/evidence/asus-maintenance/review-2026-10-09-r2"
REMOTE = "/var/tmp/api-quota-broker-maintenance-review-2026-10-09-r2"
EXTRA = {
    "maintenance_ops.py",
    "maintenance_protocol.py",
    "release_verifier.py",
    "maintenance-profile.json",
    "project.whl",
    "broker-v1.service",
    "client-v1.service",
    "export_client_credential.py",
    "aqb",
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError("template_changed")
    return text.replace(old, new)


def entry_source():
    text = (SOURCE / "history_ops_entry.py").read_text()
    old = '("inspect", "restart", "history_audit")'
    new = '("inspect", "restart", "history_audit", "preflight", "deploy", "rollback", "operation_status")'
    text = text.replace(old, new)
    text = replace(
        text,
        "releases/release-86d57624bd2f3d25ac5093355b7d5a1f37807097790257edbb3289dd40370770",
        "releases/release-e3afb8a018864885723c67a43621c56a0265eb1d0028add4ec49c74892a072cb",
    )
    text = replace(
        text,
        '    "ops_history_projection.py",\n}',
        '    "ops_history_projection.py",\n'
        + "".join("    " + repr(n) + ",\n" for n in sorted(EXTRA))
        + "}",
    )
    text = replace(
        text,
        'PACKAGE_UNITS = {"api-quota-broker-ops@.service", "api-quota-broker-history-audit@.service"}',
        'PACKAGE_UNITS = {"api-quota-broker-ops@.service", "api-quota-broker-history-audit@.service", "api-quota-broker-maintenance@.service"}',
    )
    text = replace(
        text,
        'require(name in (SERVICE, "orderflow.service"), "service_denied")',
        'require(name in (SERVICE, "orderflow.service", "ssh.service"), "service_denied")',
    )
    # Wheel is data, bounded separately; load_public_module called only with fixed .py names.
    text = replace(
        text,
        "read_root(BASE / name, sha=digest, mode=0o644)",
        "read_root(BASE / name, sha=digest, mode=0o644, limit=2097152)",
    )
    text = replace(
        text,
        "def broker_pins(config_sha):\n",
        """def broker_pins(config_sha):
    active = strict_json(read_root(CONFIG / "active-release.json", mode=0o644))
    require(set(active)=={"release","unit_sha256"}, "release_changed")
    RELEASE = active["release"]
    require(type(RELEASE) is str and re.fullmatch("releases/release-[a-f0-9]{64}", RELEASE), "release_changed")
    MANIFEST_SHA = RELEASE.rsplit("-", 1)[1]
    UNIT_SHA = active["unit_sha256"]
    require(type(UNIT_SHA) is str and re.fullmatch("[a-f0-9]{64}", UNIT_SHA), "pin_changed")
""",
    )
    text = replace(
        text,
        "    package_check()\n    pins()\n",
        """    package_check()
    if req["operation"] in ("preflight", "deploy", "restart", "rollback", "operation_status"):
        return load_public_module("maintenance_ops.py", "aqb_maintenance").dispatch(sys.modules[__name__], req)
    pins()
""",
    )
    # sys.modules lookup cannot be assumed with spec loaders: use an explicit namespace.
    text = text.replace(
        "dispatch(sys.modules[__name__], req)",
        'dispatch(__import__("types").SimpleNamespace(**globals()), req)',
    )
    text = replace(
        text,
        '        broker_pins(d["config_sha256"])\n        os.write(1, READY)',
        "        os.write(1, READY)",
    )
    text = replace(
        text, '        require(req["operation"] != "restart", "restart_not_enabled")\n', ""
    )
    text = text.replace(
        'and req["operation"] == "restart",',
        'and req["operation"] in ("deploy", "restart", "rollback"),',
    )
    insertion = """        if req["operation"] in ("preflight", "deploy", "restart", "rollback", "operation_status") and type(result) is dict and "maintenance_state" in result:
            require(self.p.returncode == 0, "operation_unknown")
            return load_public_module("maintenance_protocol.py", "aqb_maintenance_wire").validate(result, req)
"""
    text = replace(
        text,
        "        result = strict_json(raw)\n        if (",
        "        result = strict_json(raw)\n" + insertion + "        if (",
    )
    text = replace(text, "def client(operation):", "def client(operation, request_id=None):")
    text = replace(
        text,
        '{"operation": operation, "request_id": uuid.uuid4().hex}',
        '{"operation": operation, "request_id": request_id or (uuid.uuid4().hex if operation in ("inspect", "history_audit") else "")}',
    )
    text = replace(
        text,
        '        stage = "client_socket"\n',
        '        request(json.dumps(req).encode())\n        stage = "client_socket"\n',
    )
    text = replace(
        text,
        '            if operation == "history_audit" and ("summary" in result or "check" in result):',
        """            if operation in ("preflight", "deploy", "restart", "rollback", "operation_status") and "maintenance_state" in result:
                result = load_public_module("maintenance_protocol.py", "aqb_maintenance_client").validate(result, req)
                print(json.dumps(result, sort_keys=True))
                return 0 if result["maintenance_state"] in ("passed", "pending", "running") else 1
            if operation == "history_audit" and ("summary" in result or "check" in result):""",
    )
    text = replace(
        text,
        '        elif len(sys.argv) == 3 and sys.argv[1] == "client":\n            return client(sys.argv[2])',
        '        elif len(sys.argv) in (3, 4) and sys.argv[1] == "client":\n            return client(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)',
    )
    text = replace(
        text,
        '"operations": ["inspect", "history_audit"],',
        '"operations": ["inspect", "history_audit", "preflight", "deploy", "restart", "rollback", "operation_status"],',
    )
    text = replace(text, '"restart_enabled": False,', '"restart_enabled": True,')
    return text.encode()


def packet():
    files = {"ops_entry.py": entry_source()}
    for n in (
        "broker_ops_policy.py",
        "history_audit_protocol.py",
        "history_audit_reader.py",
        "ops_history_projection.py",
        "maintenance_ops.py",
        "maintenance_protocol.py",
        "export_client_credential.py",
        "aqb",
        "bootstrap_maintenance.py",
        "verify_maintenance_offline.py",
    ):
        files[n] = (SOURCE / n).read_bytes()
    reader = files["history_audit_reader.py"].decode()
    reader = replace(
        reader,
        '            "ops_history_projection.py",\n        },',
        '            "ops_history_projection.py",\n'
        + "".join("            " + repr(n) + ",\n" for n in sorted(EXTRA))
        + "        },",
    )
    files["history_audit_reader.py"] = reader.encode()
    files["release_verifier.py"] = (ROOT / "scripts/build_asus_release.py").read_bytes()
    files["project.whl"] = (NORMAL / "project.whl").read_bytes()
    files["broker-v1.service"] = (SOURCE / "api-quota-broker-v1.service").read_bytes()
    files["client-v1.service"] = (
        (SOURCE / "api-quota-broker-client.service")
        .read_bytes()
        .replace(
            b"/opt/api-quota-broker/current/deploy/asus/export_client_credential.py",
            b"/usr/local/lib/api-quota-broker-ops/export_client_credential.py",
        )
    )
    files["api-quota-broker-ops@.service"] = (
        SOURCE / "history-api-quota-broker-ops@.service"
    ).read_bytes()
    files["api-quota-broker-history-audit@.service"] = (
        SOURCE / "api-quota-broker-history-audit@.service"
    ).read_bytes()
    files["api-quota-broker-maintenance@.service"] = (
        SOURCE / "api-quota-broker-maintenance@.service"
    ).read_bytes()
    files["api-quota-broker-ops-client"] = (
        b'#!/bin/sh\ntest "$#" -ge 1 && test "$#" -le 2 || exit 1\nexec /usr/bin/python3.14 -I -B -S /usr/local/lib/api-quota-broker-ops/ops_entry.py client "$@"\n'
    )
    old = json.loads((NORMAL / "policy.json").read_bytes())
    release = "releases/release-" + old["payload_manifest_sha256"]
    files["maintenance-profile.json"] = json.dumps(
        {
            "schema": 1,
            "profile": "normal-v1",
            "old_release": "releases/release-e3afb8a018864885723c67a43621c56a0265eb1d0028add4ec49c74892a072cb",
            "incoming": "/var/tmp/api-quota-broker-normal-v1-review-2026-10-09-r2/source.tar",
            "archive_sha256": sha((NORMAL / "source.tar").read_bytes()),
            "manifest_sha256": old["payload_manifest_sha256"],
            "wheel_sha256": sha(files["project.whl"]),
            "release_unit_pins": {
                "releases/release-e3afb8a018864885723c67a43621c56a0265eb1d0028add4ec49c74892a072cb": "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060",
                release: sha(files["broker-v1.service"]),
            },
        },
        sort_keys=True,
    ).encode()
    sources = set(EXTRA) | {
        "ops_entry.py",
        "broker_ops_policy.py",
        "history_audit_protocol.py",
        "history_audit_reader.py",
        "ops_history_projection.py",
    }
    units = {n for n in files if n.startswith("api-quota-broker-") and n.endswith("@.service")}
    files["active-release.json"] = json.dumps(
        {
            "release": "releases/release-e3afb8a018864885723c67a43621c56a0265eb1d0028add4ec49c74892a072cb",
            "unit_sha256": "6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060",
        },
        sort_keys=True,
    ).encode()
    files["manifest.json"] = json.dumps(
        {
            "schema": 2,
            "files": {n: sha(files[n]) for n in sorted(sources)},
            "units": {n: sha(files[n]) for n in sorted(units)},
        },
        sort_keys=True,
    ).encode()
    files["seal.json"] = json.dumps(
        {n: sha(raw) for n, raw in sorted(files.items())}, sort_keys=True
    ).encode()
    return files


def build():
    files = packet()
    DEST.mkdir(parents=True, mode=0o700, exist_ok=False)
    # sudo copies all bounded regular files first, verifies them in the root-only
    # directory, then executes the reviewed bootstrap. No public Python import.
    pins = {n: sha(raw) for n, raw in files.items()}
    code = """import hashlib,os,stat,tempfile,subprocess,sys
from pathlib import Path
pins=PIN_LITERAL
source=Path(REMOTE_LITERAL)
root=Path(tempfile.mkdtemp(prefix='aqb-maintenance-bootstrap-',dir='/var/tmp'))
for name,sha in pins.items():
 fd=os.open(source/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as stream:
  s=os.fstat(stream.fileno())
  assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==1000 and s.st_size<=2097152
  data=stream.read(2097153)
  assert len(data)==s.st_size and hashlib.sha256(data).hexdigest()==sha
 with open(root/name,'xb') as out:
  os.chmod(root/name,0o600);out.write(data);out.flush();os.fsync(out.fileno())
result=subprocess.run(['/usr/bin/python3.14','-I','-B','-S',str(root/'bootstrap_maintenance.py')],env={'PATH':'/usr/bin:/bin','LANG':'C'})
sys.exit(result.returncode)
""".replace("PIN_LITERAL", repr(pins)).replace("REMOTE_LITERAL", repr(REMOTE))
    # Literal single quoting, never JSON masquerading as shell escaping.
    quoted = "'" + code.replace("'", "'\"'\"'") + "'"
    files["bootstrap-once.sh"] = (
        '#!/bin/sh\nset -eu\ntest "$#" -eq 0\nexec sudo /usr/bin/python3.14 -I -B -S -c '
        + quoted
        + "\n"
    ).encode()
    for name, raw in files.items():
        (DEST / name).write_bytes(raw)
        (DEST / name).chmod(0o600)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(raw))
    outer = DEST.with_suffix(".tar")
    outer.write_bytes(buf.getvalue())
    outer.chmod(0o600)
    receipt = {
        "schema": 1,
        "profile": "normal-v1",
        "files": {n: sha(r) for n, r in sorted(files.items())},
        "archive_sha256": sha(buf.getvalue()),
        "remote": REMOTE,
        "root_installed": False,
        "provider_posts": 0,
    }
    DEST.parent.joinpath("seal-2026-10-09-r2.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"files": len(files), "archive_sha256": receipt["archive_sha256"]}))


if __name__ == "__main__":
    build()
