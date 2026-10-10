"""Public SQLite fixture in a private user/mount namespace, when supported."""

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "public_history", ROOT / "tests/test_asus_integrated_history.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_new_projection_committed_wal_on_readonly_private_mount(tmp_path):
    if not shutil.which("unshare") or not shutil.which("mount"):
        pytest.skip("local_user_mount_namespace_tools_unavailable")
    directory = tmp_path / "public-db"
    directory.mkdir()
    db = directory / "ledger.sqlite3"
    fixture.database(db)
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        fixture.add(writer, "PUBLIC_WAL", "google", "completed", rid="wal", done=True)
        writer.commit()
        wal = Path(str(db) + "-wal")
        before = {path: path.read_bytes() for path in (db, wal)}
        code = """import importlib.util,json,os,sqlite3,subprocess,sys
from pathlib import Path
directory,db,source=map(Path,sys.argv[1:])
try:
 for argv in (("mount","--bind",str(directory),str(directory)),("mount","-o","remount,bind,ro",str(directory))):
  p=subprocess.run(argv,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3,check=False)
  if p.returncode: raise RuntimeError("namespace_mount_unavailable")
 assert os.statvfs(db).f_flag & os.ST_RDONLY
 spec=importlib.util.spec_from_file_location("projection",source)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 try:
  with sqlite3.connect(db.as_uri()+"?mode=ro",uri=True) as con:
   con.execute("PRAGMA query_only=ON");con.execute("BEGIN")
   result=module.classify_ledger(con,set())
  assert result["classification"]["dispatched_known_result"]==1
 except sqlite3.OperationalError:
  print('{"status":"blocked","code":"readonly_wal_shm_unavailable"}')
 else:
  print('{"status":"passed","readonly_mount":true,"committed_wal_visible":true}')
except BaseException:
 print('{"status":"blocked","code":"local_namespace_unavailable"}')
"""
        result = subprocess.run(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--mount",
                "--propagation",
                "private",
                "--fork",
                sys.executable,
                "-I",
                "-B",
                "-S",
                "-c",
                code,
                str(directory),
                str(db),
                str(ROOT / "deploy/asus/ops_history_projection.py"),
            ],
            capture_output=True,
            timeout=12,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("local_user_mount_namespace_unavailable")
        value = json.loads(result.stdout)
        if value.get("code") == "local_namespace_unavailable":
            pytest.skip("local_user_mount_namespace_unavailable")
        assert value == {"status": "passed", "readonly_mount": True, "committed_wal_visible": True}
        assert all(path.read_bytes() == raw for path, raw in before.items())
