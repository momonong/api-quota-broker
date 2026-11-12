"""One fixed HP Dashboard read/plan attachment; no root application imports.

The old publisher keeps its exact kernel argv and helper pin. Only its launcher,
unit credential directive and corresponding two policy hashes change. A durable
fixed operation blocks blind retries; rollback never overwrites foreign changes.
"""

import argparse
import ast
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import resource
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OWNER = 0
LAUNCHER = Path("/usr/local/libexec/momonong-quota-launcher.py")
UNIT = Path("/etc/systemd/system/api-quota-dashboard.service")
POLICY = Path("/etc/momonong-quota-maintenance/policy.json")
HELPER = Path("/usr/local/libexec/hp-quota-maintenance.py")
CREDENTIAL = Path("/etc/credstore.encrypted/quota-dashboard-broker-read")
STATE = Path("/var/lib/quota-shared-dashboard-attach-20261111")
LOCK = Path("/run/momonong-quota-update.lock")
SERVICE = "api-quota-dashboard.service"
OPERATION = "hp-quota-shared-dashboard-attach-20261111-v1"
OLD_LAUNCHER = "f49e55b0c2901122b96d7f3f025e66a1d18242a6d5b28c72170a40d7a3b08176"
OLD_UNIT = "4eca3f6ff46d70c3299f51acfd13170f0076a14c120d2d04fcfb8ee97a66eb70"
CREDENTIAL_LINE = (
    "LoadCredentialEncrypted=broker-read:/etc/credstore.encrypted/quota-dashboard-broker-read"
)
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
MAXIMUM = 131072


class Refused(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise Refused(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, "duplicate_json")
        value[key] = item
    return value


def parse(raw):
    need(len(raw) <= MAXIMUM, "input_bound")
    return json.loads(raw, object_pairs_hook=unique)


def read(path, *, mode=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == OWNER
            and info.st_nlink == 1
            and not info.st_mode & 0o022
            and 0 < info.st_size <= MAXIMUM
            and (mode is None or stat.S_IMODE(info.st_mode) == mode),
            "file_permissions",
        )
        raw = stream.read(MAXIMUM + 1)
        after = os.fstat(stream.fileno())
        need(
            (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and len(raw) == info.st_size,
            "file_drift",
        )
    return raw


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path.parent)


def replace(path, raw, mode):
    temporary = path.with_name(path.name + ".quota-shared-attach-tmp")
    write_new(temporary, raw, mode)
    os.replace(temporary, path)
    fsync_dir(path.parent)


def targets():
    return {"launcher": LAUNCHER, "unit": UNIT, "policy": POLICY}


def command(argv, *, data=None, timeout=30):
    result = subprocess.run(
        argv,
        input=data,
        stdin=subprocess.DEVNULL if data is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=ENV,
        timeout=timeout,
        check=False,
    )
    need(result.returncode == 0, "service_command_failed")
    return result.stdout


def encrypt_token(token):
    raw = command(
        ["/usr/bin/systemd-creds", "encrypt", "--with-key=host", "--name=broker-read", "-", "-"],
        data=token.encode(),
        timeout=15,
    )
    need(0 < len(raw) <= 32768, "credential_encryption_failed")
    return raw


def broker_request(token):
    request = urllib.request.Request(
        "http://127.0.0.1:18587/v1/catalog", headers={"Authorization": "Bearer " + token}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=5) as response:
        need(response.status == 200, "broker_not_ready")
        raw = response.read(2 * 1024 * 1024 + 1)
    need(len(raw) <= 2 * 1024 * 1024 and isinstance(json.loads(raw), list), "broker_not_ready")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


def verify_broker(token):
    base = Path("/opt/quota-shared-broker")
    for directory in (base, base / "releases"):
        fixed_parent(directory)
    current = base / "current"
    info = current.lstat()
    need(stat.S_ISLNK(info.st_mode) and info.st_uid == OWNER, "broker_release_owner")
    relative = os.readlink(current)
    need(re.fullmatch(r"releases/[a-z0-9][a-z0-9_-]{0,95}", relative), "broker_release_name")
    release = base / relative
    fixed_parent(release)
    fixed_parent(release / "config")
    check_read_policy(release / "config/access.json", token)
    broker_request(token)


def check_read_policy(path, token):
    policy = parse(read(path))
    need(
        isinstance(policy, dict)
        and policy.get("version") == 1
        and isinstance(policy.get("principals"), list),
        "broker_policy",
    )
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    matches = [
        principal
        for principal in policy["principals"]
        if isinstance(principal, dict)
        and isinstance(principal.get("token_sha256"), str)
        and hmac.compare_digest(principal["token_sha256"], token_hash)
    ]
    need(
        len(matches) == 1
        and matches[0].get("project_id") is None
        and isinstance(matches[0].get("scopes"), list)
        and sorted(matches[0]["scopes"]) == ["plan", "read"],
        "dashboard_read_plan_scope_required",
    )


def fixed_parent(path):
    info = path.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022,
        "fixed_parent_permissions",
    )


def restart_dashboard():
    command(["/usr/bin/systemctl", "daemon-reload"])
    command(["/usr/bin/systemctl", "restart", SERVICE], timeout=60)


def verify_host_contract(policy):
    values = dict(
        line.split("=", 1)
        for line in command(
            [
                "/usr/bin/systemctl",
                "show",
                SERVICE,
                "--property=User,Group,FragmentPath,DropInPaths",
            ],
            timeout=5,
        )
        .decode()
        .splitlines()
    )
    need(
        values
        == {
            "User": policy["service_user"],
            "Group": policy["service_group"],
            "FragmentPath": str(UNIT),
            "DropInPaths": "",
        },
        "service_contract_drift",
    )
    need(policy["service_user"] != "root" and policy["service_group"] != "root", "service_identity")


def verify_dashboard():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            values = dict(
                line.split("=", 1)
                for line in command(
                    [
                        "/usr/bin/systemctl",
                        "show",
                        SERVICE,
                        "--property=ActiveState,SubState,MainPID",
                    ],
                    timeout=5,
                )
                .decode()
                .splitlines()
            )
            need(
                values.get("ActiveState") == "active"
                and values.get("SubState") == "running"
                and values.get("MainPID", "0").isdigit()
                and int(values["MainPID"]) > 0,
                "dashboard_not_ready",
            )
            request = urllib.request.Request(
                "http://127.0.0.1:18586/quota/", headers={"Host": "momonong.me"}
            )
            with opener.open(request, timeout=3) as response:
                need(response.status == 200, "dashboard_not_ready")
            try:
                request = urllib.request.Request(
                    "http://127.0.0.1:18586/quota/api/snapshot", headers={"Host": "momonong.me"}
                )
                opener.open(request, timeout=3)
            except urllib.error.HTTPError as error:
                need(error.code == 401, "dashboard_auth_failed")
            else:
                raise Refused("dashboard_auth_failed")
            return
        except (OSError, ValueError):
            time.sleep(0.2)
    raise Refused("dashboard_not_ready")


def make_unit(raw):
    text = raw.decode("utf-8")
    need(
        text.count("[Service]\n") == 1
        and CREDENTIAL_LINE not in text
        and "LoadCredentialEncrypted=broker-read:" not in text,
        "unit_contract",
    )
    # The legacy kernel argv is deliberately retained for the immutable helper.
    need(
        text.count("ExecStart=") == 1
        and " /usr/local/libexec/momonong-quota-launcher.py dashboard-serve " in text,
        "unit_contract",
    )
    return text.replace("[Service]\n", "[Service]\n" + CREDENTIAL_LINE + "\n", 1).encode()


def prepare(payload, launcher_sha256, *, apply=False):
    need(
        isinstance(payload, dict)
        and set(payload) == ({"launcher_base64", "read_token"} if apply else {"launcher_base64"}),
        "payload_fields",
    )
    need(
        isinstance(launcher_sha256, str)
        and len(launcher_sha256) == 64
        and all(char in "0123456789abcdef" for char in launcher_sha256),
        "launcher_pin",
    )
    raw = base64.b64decode(payload["launcher_base64"], validate=True)
    need(0 < len(raw) <= 65536 and digest(raw) == launcher_sha256, "launcher_pin")
    ast.parse(raw.decode(), filename="reviewed-dashboard-launcher.py")
    if apply:
        token = payload["read_token"]
        need(
            isinstance(token, str)
            and 32 <= len(token) <= 1024
            and all(33 <= ord(char) <= 126 for char in token),
            "read_token_format",
        )
    old = {
        name: read(path, mode=0o600 if name == "policy" else 0o644)
        for name, path in targets().items()
    }
    need(
        digest(old["launcher"]) == OLD_LAUNCHER and digest(old["unit"]) == OLD_UNIT,
        "baseline_drift",
    )
    policy = parse(old["policy"])
    need(
        isinstance(policy, dict)
        and set(policy)
        == {
            "schema",
            "helper_sha256",
            "launcher_sha256",
            "unit_sha256",
            "service_user",
            "service_group",
        }
        and policy["schema"] == "hp-quota-maintenance-policy-v1"
        and policy["launcher_sha256"] == OLD_LAUNCHER
        and policy["unit_sha256"] == OLD_UNIT
        and policy["helper_sha256"] == digest(read(HELPER)),
        "maintenance_policy_drift",
    )
    unit = make_unit(old["unit"])
    new_policy = {**policy, "launcher_sha256": digest(raw), "unit_sha256": digest(unit)}
    return {
        "launcher": raw,
        "unit": unit,
        "policy": canonical(new_policy),
        "old": old,
        "helper_sha256": policy["helper_sha256"],
    }


def save_receipt(value, *, initial=False):
    raw = canonical(value)
    if initial:
        write_new(STATE / "intent.json", raw)
    else:
        replace(STATE / "intent.json", raw, 0o600)


def status():
    if not STATE.exists():
        return {"operation": OPERATION, "status": "not_started", "provider_calls": 0}
    info = STATE.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and stat.S_IMODE(info.st_mode) == 0o700,
        "receipt_permissions",
    )
    if not (STATE / "intent.json").exists():
        return {"operation": OPERATION, "status": "needs_reconcile", "provider_calls": 0}
    receipt = parse(read(STATE / "intent.json", mode=0o600))
    need(receipt.get("operation") == OPERATION, "original_receipt_required")
    return receipt


def rollback():
    receipt = status()
    need(
        receipt["status"] != "not_started"
        and {"old_hashes", "new_hashes", "helper_sha256"} <= set(receipt),
        "original_receipt_required",
    )
    old = {name: read(STATE / (name + ".original"), mode=0o600) for name in targets()}
    for name, path in targets().items():
        need(
            digest(old[name]) == receipt["old_hashes"][name]
            and digest(read(path)) in {receipt["old_hashes"][name], receipt["new_hashes"][name]},
            "foreign_change_no_rollback",
        )
    need(digest(read(HELPER)) == receipt["helper_sha256"], "helper_drift_no_rollback")
    receipt["status"] = "rolling_back"
    save_receipt(receipt)
    for name, path in targets().items():
        replace(path, old[name], 0o600 if name == "policy" else 0o644)
    restart_dashboard()
    verify_dashboard()
    receipt["status"] = "rolled_back"
    save_receipt(receipt)
    return receipt


def apply(candidate, token):
    need(not STATE.exists() and not STATE.is_symlink(), "inspect_original_receipt_do_not_replay")
    need(not CREDENTIAL.exists() and not CREDENTIAL.is_symlink(), "credential_already_exists")
    # A fresh read verifies the reviewed baseline before claiming the operation.
    for name, path in targets().items():
        need(read(path) == candidate["old"][name], "baseline_drift")
    verify_broker(token)
    encrypted = encrypt_token(token)
    for name, path in targets().items():
        need(read(path) == candidate["old"][name], "baseline_drift")
    STATE.mkdir(mode=0o700)
    fsync_dir(STATE.parent)
    receipt = {
        "operation": OPERATION,
        "schema": 1,
        "status": "preparing",
        "provider_calls": 0,
        "old_hashes": {name: digest(raw) for name, raw in candidate["old"].items()},
        "new_hashes": {name: digest(candidate[name]) for name in targets()},
        "helper_sha256": candidate["helper_sha256"],
    }
    save_receipt(receipt, initial=True)
    for name, raw in candidate["old"].items():
        write_new(STATE / (name + ".original"), raw)
    receipt["status"] = "prepared"
    save_receipt(receipt)
    try:
        write_new(CREDENTIAL, encrypted)
        receipt["status"] = "installing"
        save_receipt(receipt)
        for name, path in targets().items():
            need(read(path) == candidate["old"][name], "installed_baseline_drift")
            replace(path, candidate[name], 0o600 if name == "policy" else 0o644)
        receipt["status"] = "restarting"
        save_receipt(receipt)
        restart_dashboard()
        verify_dashboard()
        for name, path in targets().items():
            need(digest(read(path)) == receipt["new_hashes"][name], "installed_pin_drift")
        need(digest(read(HELPER)) == receipt["helper_sha256"], "helper_drift")
        receipt["status"] = "complete"
        save_receipt(receipt)
        return receipt
    except Exception as error:  # noqa: BLE001 - never reflect private exception messages
        receipt["failure_phase"] = receipt["status"]
        receipt["failure_class"] = type(error).__name__
        save_receipt(receipt)
        try:
            return rollback()
        except Exception:  # noqa: BLE001 - evidence remains; original operation must reconcile
            receipt["status"] = "needs_reconcile"
            save_receipt(receipt)
            return receipt


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    need(
        os.geteuid() == 0 and socket.gethostname() == "hp-ubuntu2604-server", "fixed_hp_root_entry"
    )
    need(not os.environ.get("SSH_ORIGINAL_COMMAND"), "forced_command_not_authorized")
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    for name in ("validate", "apply", "status", "rollback"):
        action.add_argument("--" + name, action="store_true")
    parser.add_argument("--launcher-sha256")
    args = parser.parse_args()
    for path in (*targets().values(), HELPER, CREDENTIAL, STATE, LOCK):
        for parent in path.parents:
            fixed_parent(parent)
    fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b") as lock:
        info = os.fstat(lock.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "lock_permissions",
        )
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.status:
            result = status()
        elif args.rollback:
            result = rollback()
        else:
            payload = parse(sys.stdin.buffer.read(MAXIMUM + 1))
            candidate = prepare(payload, args.launcher_sha256, apply=args.apply)
            verify_host_contract(parse(candidate["old"]["policy"]))
            if args.apply:
                result = apply(candidate, payload["read_token"])
            else:
                result = {
                    "operation": OPERATION,
                    "status": "validated",
                    "provider_calls": 0,
                    "launcher_sha256": digest(candidate["launcher"]),
                    "unit_sha256": digest(candidate["unit"]),
                    "policy_sha256": digest(candidate["policy"]),
                }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - no secret, payload, error string or traceback in output
        print(
            json.dumps(
                {
                    "operation": OPERATION,
                    "status": "stopped",
                    "reason": "inspect_original_receipt_do_not_replay",
                    "provider_calls": 0,
                }
            )
        )
        raise SystemExit(1) from None
