"""Bounded ASUS root metadata classification, with no file content reads.

No importer, host-key setup, credential decrypt, hashing of keys/ciphertexts,
stage mutation, provider call, systemctl mutation or retry is performed.
"""

import json
import os
import re
import socket
import stat
import sys
from pathlib import Path

NAMES = {"digest_key", "client_token", "admin_token", "queue_key", "doppler_service_token"}
CONFIG = Path("/etc/api-quota-broker")
STAGE = re.compile(r"\.credential-init\.[0-9a-f]{32}\Z")


class DiagnosticError(ValueError):
    pass


def metadata(path: Path) -> dict:
    for parent in (*reversed(path.parents), path):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return {"present": False}
        if stat.S_ISLNK(info.st_mode):
            raise DiagnosticError
    return {
        "present": True,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
        "size": info.st_size,
        "kind": "file"
        if stat.S_ISREG(info.st_mode)
        else "directory"
        if stat.S_ISDIR(info.st_mode)
        else "other",
    }


def entries(directory: Path) -> list[Path]:
    values = []
    for value in directory.iterdir():
        if len(values) == 32:
            raise DiagnosticError
        values.append(value)
    return values


def report() -> dict:
    if (
        os.geteuid() != 0
        or socket.gethostname() != "asus-ubuntu2604-server"
        or not sys.stdin.isatty()
    ):
        raise DiagnosticError
    result = {
        "status": "passed",
        "mode": "credential_metadata_diagnostic",
        "credential_reads": 0,
        "provider_calls": 0,
        "runtime_changes": 0,
        "service_changes": 0,
        "stages": [],
    }
    config = metadata(CONFIG)
    if config.get("kind") != "directory" or config["uid"] != 0 or config["mode"] != 0o750:
        raise DiagnosticError
    result["config"] = config
    directory = CONFIG / "credentials"
    result["credentials_directory"] = metadata(directory)
    children = entries(directory)
    result["credentials_entries"] = len(children)
    result["credential_file_metadata"] = {
        p.name: metadata(p)
        for p in children
        if p.name
        in {name + ".cred" for name in NAMES} | {"doppler-metadata.json", "import-evidence.json"}
    }
    result["unknown_credential_entries"] = sum(
        p.name not in result["credential_file_metadata"] for p in children
    )
    result["state_directory"] = metadata(Path("/var/lib/api-quota-broker"))
    result["state_entries"] = len(entries(Path("/var/lib/api-quota-broker")))
    result["systemd_creds"] = metadata(Path("/usr/bin/systemd-creds"))
    result["host_key"] = metadata(Path("/var/lib/systemd/credential.secret"))
    host = result["host_key"]
    result["host_key_metadata_ready"] = (
        host.get("kind") == "file"
        and host["uid"] == 0
        and host["mode"] in (0o400, 0o600)
        and host["nlink"] == 1
        and host["size"] > 0
    )
    config_entries = sorted(entries(CONFIG))
    result["unrecognized_stage_entries"] = sum(
        p.name.startswith(".credential-init.") and not STAGE.fullmatch(p.name)
        for p in config_entries
    )
    for path in config_entries:
        if not STAGE.fullmatch(path.name):
            continue
        row = {"name": path.name, "metadata": metadata(path)}
        if (
            row["metadata"]["kind"] != "directory"
            or row["metadata"]["uid"] != 0
            or row["metadata"]["mode"] != 0o700
        ):
            raise DiagnosticError
        children = entries(path)
        row["entry_count"] = len(children)
        row["ciphertext_files"] = sum(
            p.name in {name + ".cred" for name in NAMES} for p in children
        )
        row["unknown_entries"] = sum(
            p.name
            not in {name + ".cred" for name in NAMES}
            | {"import-evidence.json", "doppler-metadata.json"}
            for p in children
        )
        row["entry_metadata"] = {
            p.name: metadata(p)
            for p in children
            if p.name
            in {name + ".cred" for name in NAMES}
            | {"import-evidence.json", "doppler-metadata.json"}
        }
        result["stages"].append(row)
    return result


if __name__ == "__main__":
    try:
        value = report()
    except (DiagnosticError, OSError, ValueError):
        value = {
            "status": "failed",
            "reason": "metadata_diagnostic_gate_failed",
            "credential_reads": 0,
            "provider_calls": 0,
            "service_changes": 0,
        }
    print(json.dumps(value, sort_keys=True))
    raise SystemExit(value["status"] != "passed")
