"""File transaction and irreversible-dispatch boundaries; not native UID tests."""

import importlib.util
import json
import os
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/asus" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r = load("repair_maintenance")
ops = load("maintenance_ops")
protocol = load("maintenance_protocol")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for key, relative in {
        "BASE": "ops",
        "STATE": "state",
        "APP": "app",
        "SYSTEM": "system",
    }.items():
        monkeypatch.setattr(r, key, tmp_path / relative)
        (tmp_path / relative).mkdir()
    monkeypatch.setattr(r, "BACKUP", r.STATE / "repair-setuid-r1.backup")
    monkeypatch.setattr(r, "CLAIM", r.STATE / "repair-setuid-r1.claim.json")
    monkeypatch.setattr(r, "RESULT", r.STATE / "repair-setuid-r1.result.json")
    sealed = ROOT / "tests/fixtures/asus-history/maintenance-r2"
    original = {
        r.BASE / "maintenance_ops.py": (sealed / "maintenance_ops.py").read_bytes(),
        r.BASE / "manifest.json": (sealed / "manifest.json").read_bytes(),
        r.SYSTEM / r.UNIT: (sealed / r.UNIT).read_bytes(),
    }
    for p, b in original.items():
        p.write_bytes(b)
    manifest = json.loads(original[r.BASE / "manifest.json"])
    files = {
        "maintenance_ops.py": (ROOT / "deploy/asus/maintenance_ops.py").read_bytes(),
        r.UNIT: (ROOT / "deploy/asus" / r.UNIT).read_bytes(),
    }
    manifest["files"]["maintenance_ops.py"] = r.sha(files["maintenance_ops.py"])
    manifest["units"][r.UNIT] = r.sha(files[r.UNIT])
    files["manifest.json"] = r.wire(manifest)
    req = {"operation": "deploy", "request_id": r.RID}
    receipt = ops.wire(
        protocol.receipt(req, "blocked", "internal_error", legacy_clear=True, queue_jobs=0)
    )
    (r.STATE / (r.RID + ".request.json")).write_bytes(r.wire(req))
    (r.STATE / (r.RID + ".result.json")).write_bytes(receipt)
    (r.STATE / (r.RID + ".started")).write_bytes(b"1\n")
    candidate = r.APP / ("releases/release-" + r.MANIFEST)
    candidate.mkdir(parents=True)
    # Verify true sealed manifest identity, not a synthetic manifest string.
    manifest_bytes = (
        ROOT / "tests/fixtures/asus-history/normal-r2-release-manifest.json"
    ).read_bytes()
    assert r.sha(manifest_bytes) == r.MANIFEST
    (candidate / "release-manifest.json").write_bytes(manifest_bytes)
    (candidate / "maintenance-runtime-install.json").write_bytes(
        r.wire({"manifest_sha256": r.MANIFEST, "root_candidate_execution": False})
    )
    (candidate / "original-content").write_bytes(b"preserve me")
    monkeypatch.setattr(
        r,
        "tree_fingerprint",
        lambda p: r.sha(b"".join(q.read_bytes() for q in sorted(p.rglob("*")) if q.is_file())),
    )
    calls = []

    class Entry:
        def read_root(self, p, **kw):
            return p.read_bytes()

        def strict_json(self, raw):
            return json.loads(raw)

        def write_exclusive(self, p, raw, **kw):
            with p.open("xb") as f:
                f.write(raw)

        def package(self):
            m = json.loads((r.BASE / "manifest.json").read_bytes())
            assert m["files"]["maintenance_ops.py"] == r.sha(
                (r.BASE / "maintenance_ops.py").read_bytes()
            )
            assert m["units"][r.UNIT] == r.sha((r.SYSTEM / r.UNIT).read_bytes())

        def service_state(self, name):
            return dict(
                zip(
                    ("MainPID", "ExecMainStartTimestampMonotonic", "NRestarts"),
                    r.SERVICES[name],
                    strict=True,
                )
            )

        def native(self, argv, **kw):
            calls.append(argv)
            return b""

    class Native:
        def __init__(self, *a):
            self.initial = True
            self.jobs = 0
            self.legacy_clear = True
            self.manifest_sha = r.MANIFEST

        def preflight(self):
            pass

        def upload(self):
            pass

    def atomic(e, p, raw, **kw):
        temporary = p.with_suffix(".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, p)

    old = types.SimpleNamespace(
        module=lambda *a: protocol, wire=ops.wire, Native=Native, atomic=atomic, sync=lambda p: None
    )
    return Entry(), old, files, original, candidate, calls, receipt


def test_fixed_repair_preserves_failed_stage_and_original_receipts(setup):
    entry, old, files, original, candidate, calls, receipt = setup
    record = r.repair(entry, old, files)
    assert record["stage"] == "prepared" and not record["dispatched"]
    assert not any("start" in c for c in calls)
    archived = r.APP / ("releases/failed-stage-" + r.RID)
    assert not candidate.exists() and (archived / "original-content").read_bytes() == b"preserve me"
    assert (r.STATE / (r.RID + ".result.json")).read_bytes() == receipt
    assert (r.STATE / (r.RID + ".started")).read_bytes() == b"1\n"
    index = json.loads((r.BACKUP / "index.json").read_bytes())
    for p, raw in original.items():
        assert (r.BACKUP / index[str(p)]["file"]).read_bytes() == raw
    assert r.dispatch_prepared(entry, record)["stage"] == "dispatched"
    assert sum("start" in c for c in calls) == 1
    with pytest.raises(ValueError):
        r.repair(entry, old, files)


def test_manifest_write_failure_restores_all_originals(setup):
    entry, old, files, original, candidate, calls, _ = setup
    actual = old.atomic

    def fault(e, p, raw, **kw):
        if p == r.BASE / "manifest.json" and raw == files["manifest.json"]:
            raise OSError("fixture")
        return actual(e, p, raw, **kw)

    old.atomic = fault
    outcome = r.repair(entry, old, files)
    assert outcome["state"] == "blocked" and outcome["rollback_verified"]
    assert all(p.read_bytes() == raw for p, raw in original.items())
    assert candidate.exists()
    assert not (r.STATE / (r.RID + ".repair-authorized.json")).exists()
    assert not any("start" in c for c in calls)


def test_unknown_dispatch_never_restores_or_repeats(setup):
    entry, old, files, _original, _candidate, calls, _ = setup
    record = r.repair(entry, old, files)

    def fault(argv, **kw):
        calls.append(argv)
        raise TimeoutError("sensitive fixture")

    entry.native = fault
    outcome = r.dispatch_prepared(entry, record)
    assert outcome["state"] == "unknown" and outcome["dispatched"]
    assert not outcome["rollback_verified"]
    assert sum("start" in c for c in calls) == 1
    assert "sensitive" not in str(outcome)


def test_changed_original_receipt_fails_before_writes(setup):
    entry, old, files, _, candidate, calls, _ = setup
    (r.STATE / (r.RID + ".result.json")).write_bytes(b"{}")
    with pytest.raises(ValueError):
        r.repair(entry, old, files)
    assert not r.CLAIM.exists() and candidate.exists() and calls == []
