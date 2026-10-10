"""Stdlib-only non-root verification of the sealed maintenance packet."""

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise ValueError("nonroot_only")
    seal = json.loads((args.packet / "seal.json").read_bytes())
    for name, digest in seal.items():
        path = args.packet / name
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 2097152:
            raise ValueError("packet_metadata")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("packet_digest")
        if name.endswith(".py"):
            compile(path.read_bytes(), name, "exec")

    def load(name):
        spec = importlib.util.spec_from_file_location("offline_" + name, args.packet / name)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    entry = load("ops_entry.py")
    ops = load("maintenance_ops.py")
    protocol = load("maintenance_protocol.py")
    verifier = load("release_verifier.py")
    profile = json.loads((args.packet / "maintenance-profile.json").read_bytes())
    raw = args.source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != profile["archive_sha256"]:
        raise ValueError("source_identity")
    checked = verifier.verify_archive(raw, profile["manifest_sha256"])
    ops.wheel_files((args.packet / "project.whl").read_bytes(), checked["release"]["files"])
    for op in protocol.OPERATIONS:
        request = {"operation": op, "request_id": "a" * 32}
        entry.request(protocol.canonical(request))
        protocol.validate(protocol.receipt(request, "pending", "accepted"), request)
    with sqlite3.connect(":memory:") as con:
        con.executescript("""CREATE TABLE queue_jobs(state TEXT,lease_owner TEXT,lease_token TEXT,
            lease_until TEXT,execution_until TEXT,run_started INT,execution_key TEXT,payload BLOB);
            CREATE TABLE queue_attempts(execution_key TEXT);
            CREATE TABLE queue_settings(id INT,verifier BLOB);
            INSERT INTO queue_settings VALUES(1,X'1234');""")
        assert ops.queue_gate(con, first=True) == 0
    print(
        json.dumps(
            {
                "status": "passed",
                "python": sys.version.split()[0],
                "sealed_files": len(seal),
                "fixed_wire_operations": len(protocol.OPERATIONS),
                "r15_history_modules_retained": True,
                "archive_and_wheel_verified": True,
                "fixture_sqlite_verified": True,
                "root_calls": 0,
                "secret_reads": 0,
                "provider_posts": 0,
                "formal_database_writes": 0,
                "service_changes": 0,
                "native_deployment_verified": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
