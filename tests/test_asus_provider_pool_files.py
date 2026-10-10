"""New file primitives are exercised only in an unprivileged mapped namespace."""

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires mapped namespace root")


def module():
    assert Path("/proc/self/uid_map").read_text().split()[1] != "0"
    path = Path(__file__).resolve().parents[1] / "deploy/asus/enable_provider_pool.py"
    spec = importlib.util.spec_from_file_location("pool_files", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def accessible(path):
    for directory in (path, path.parent, path.parent.parent):
        directory.chmod(0o755)


def test_umask_cannot_remove_service_group_read(tmp_path):
    m = module()
    accessible(tmp_path)
    old = os.umask(0o077)
    try:
        destination = tmp_path / "config.json"
        m.atomic_file(destination, b'{"targets":[]}', service_read=True)
        m.service_can_read(destination, m.digest(destination.read_bytes()))
        assert destination.stat().st_gid == 982
        assert destination.stat().st_mode & 0o777 == 0o640
    finally:
        os.umask(old)


def test_service_cannot_read_root_private_file(tmp_path):
    m = module()
    accessible(tmp_path)
    path = tmp_path / "private.json"
    m.write_exclusive(path, b"fixture")
    with pytest.raises(m.GateError):
        m.service_can_read(path, m.digest(b"fixture"))


def test_exclusive_claim_and_symlink_metadata_fail_closed(tmp_path):
    m = module()
    path = tmp_path / "claim.json"
    m.write_exclusive(path, b"first")
    with pytest.raises(FileExistsError):
        m.write_exclusive(path, b"second")
    assert path.read_bytes() == b"first"
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        m.read_regular(link)


def test_foreign_journal_is_not_overwritten(tmp_path, monkeypatch):
    m = module()
    path = tmp_path / "journal.json"
    monkeypatch.setattr(m, "JOURNAL", path)
    ops = m.Ops(tmp_path, {}, None, None)
    ops.save({"phase": "first"})
    path.write_bytes(b"foreign receipt")
    with pytest.raises(m.GateError):
        ops.save({"phase": "second"})
    assert path.read_bytes() == b"foreign receipt"
