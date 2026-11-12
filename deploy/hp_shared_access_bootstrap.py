"""One reviewed HP gate for shared-Broker maintenance and private SSH access.

Only public SSH keys enter the payload. The existing read/plan token enters on
stdin, is checked against the live policy, and is host-encrypted in memory.
New accounts remain expired until SSH, sudo and the fixed helper are validated.
The gate never changes the runtime, its ledger, website authentication or Caddy.
"""

import argparse
import ast
import base64
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import resource
import signal
import socket
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

OWNER = 0
DEPLOY, CLIENT = "quota-broker-deploy", "quota-broker-client"
RUNTIME_PIN = "ffa0b31461173d1d947d4d79491e2f3d7647e4aa48ecd1aeecc71bff6f9c64e0"
UNIT_PIN = "b46d207b265a3f5e20e76c5b13a8fc55c33bd1a5aaa19623afe91deee0e96467"
BASE = Path("/opt/quota-shared-broker")
RUNTIME = Path("/usr/local/libexec/quota-shared-runtime.py")
UNIT = Path("/etc/systemd/system/quota-shared-broker.service")
ENTRY = Path("/usr/local/libexec/hp-quota-shared-maintenance.py")
FORCED = Path("/usr/local/libexec/quota-shared-deploy-entry")
ROOT_ENTRY = Path("/usr/local/libexec/quota-shared-maintenance-root")
POLICY_DIR = Path("/etc/quota-shared-maintenance")
JOURNAL = Path("/var/lib/quota-shared-maintenance")
LOCK = Path("/run/quota-shared-update.lock")
GATE_LOCK = Path("/run/quota-shared-access-bootstrap.lock")
RECEIPT = Path("/var/lib/quota-shared-access-bootstrap-20261111.json")
READ_TOKEN = Path("/etc/credstore.encrypted/quota-shared-read-token")
SSH_CONFIG = Path("/etc/ssh/sshd_config")
SSH_SNIPPET = Path("/etc/ssh/sshd_config.d/92-quota-shared.conf")
SUDOERS = Path("/etc/sudoers.d/92-quota-shared")
MAX_INPUT = 2 * 1024**2
ROOT_TEXT = """#!/usr/bin/python3 -I
import os
import sys
if sys.argv[1:] or os.geteuid() != 0 or os.environ.get("SSH_ORIGINAL_COMMAND"):
    raise SystemExit(126)
os.execve("/usr/bin/python3", ["/usr/bin/python3", "-I", "-B", "/usr/local/libexec/hp-quota-shared-maintenance.py"], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
"""
FORCED_TEXT = """#!/usr/bin/python3 -I
import os
import sys
if sys.argv[1:] or os.geteuid() == 0 or os.environ.get("SSH_ORIGINAL_COMMAND"):
    raise SystemExit(126)
os.execve("/usr/bin/sudo", ["/usr/bin/sudo", "-n", "/usr/local/libexec/quota-shared-maintenance-root"], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
"""
SUDOERS_TEXT = (
    'quota-broker-deploy ALL=(root) NOPASSWD: /usr/local/libexec/quota-shared-maintenance-root ""\n'
)
SSH_TEXT = """# Fixed shared Quota identities only; leave existing users unchanged.
Match User quota-broker-deploy
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowUsers quota-broker-deploy
    AllowGroups quota-broker-deploy
    AuthorizedKeysFile /etc/quota-shared-maintenance/keys/quota-broker-deploy
    ForceCommand /usr/local/libexec/quota-shared-deploy-entry
    DisableForwarding yes
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    PermitTunnel no
    X11Forwarding no
    PermitTTY no
    PermitUserRC no
    PermitOpen none
    PermitListen none
    MaxSessions 1
Match User quota-broker-client
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowUsers quota-broker-client
    AllowGroups quota-broker-client
    AuthorizedKeysFile /etc/quota-shared-maintenance/keys/quota-broker-client
    ForceCommand /usr/bin/false
    DisableForwarding no
    AllowTcpForwarding local
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    PermitTunnel no
    X11Forwarding no
    PermitTTY no
    PermitUserRC no
    PermitOpen 127.0.0.1:18588
    PermitListen none
    MaxSessions 0
Match all
"""


class Refused(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise Refused(code)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, "duplicate_json")
        value[key] = item
    return value


def parse(raw):
    need(0 < len(raw) <= MAX_INPUT, "input_bound")
    try:
        return json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(Refused("json_constant")),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise Refused("json_invalid") from None


def public_key(value):
    need(
        isinstance(value, str) and re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", value),
        "public_key",
    )
    try:
        raw = base64.b64decode(value.split(" ")[1], validate=True)
    except ValueError:
        raise Refused("public_key") from None
    kind = b"ssh-ed25519"
    need(
        raw == len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + raw[-32:]
        and len(raw) == 4 + len(kind) + 4 + 32,
        "public_key",
    )
    return value


def validate_payload(value, payload_pin, helper_pin):
    need(
        isinstance(value, dict)
        and set(value)
        == {
            "schema",
            "operation_id",
            "helper",
            "deploy_public_key",
            "client_public_key",
            "runtime_launcher_sha256",
            "unit_sha256",
        }
        and value["schema"] == "hp-quota-shared-access-v1",
        "payload_schema",
    )
    need(
        isinstance(payload_pin, str)
        and re.fullmatch(r"[0-9a-f]{64}", payload_pin)
        and digest(canonical(value)) == payload_pin,
        "payload_pin",
    )
    try:
        need(str(UUID(value["operation_id"])) == value["operation_id"], "operation_id")
    except (ValueError, TypeError, AttributeError):
        raise Refused("operation_id") from None
    public_key(value["deploy_public_key"])
    public_key(value["client_public_key"])
    need(value["deploy_public_key"] != value["client_public_key"], "distinct_keys_required")
    need(
        value["runtime_launcher_sha256"] == RUNTIME_PIN and value["unit_sha256"] == UNIT_PIN,
        "runtime_pins",
    )
    helper = value["helper"]
    need(
        isinstance(helper, dict)
        and set(helper) == {"size", "sha256", "content_base64"}
        and type(helper["size"]) is int
        and 0 < helper["size"] <= 1024**2
        and isinstance(helper["content_base64"], str)
        and len(helper["content_base64"]) <= 1400000
        and isinstance(helper_pin, str)
        and re.fullmatch(r"[0-9a-f]{64}", helper_pin),
        "helper_blob",
    )
    try:
        raw = base64.b64decode(helper["content_base64"], validate=True)
        ast.parse(raw, filename="reviewed-maintenance-helper")
    except (ValueError, SyntaxError, RecursionError):
        raise Refused("helper_blob") from None
    need(len(raw) == helper["size"] and digest(raw) == helper["sha256"] == helper_pin, "helper_pin")
    return {"helper": raw, "payload_sha256": payload_pin, "helper_sha256": helper_pin}


def root_directory(path):
    for item in (path, *path.parents):
        info = item.lstat()
        need(
            stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022,
            "root_directory",
        )


def read(path, bound=262144, *, mode=None):
    root_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == OWNER
            and before.st_nlink == 1
            and not before.st_mode & 0o022
            and before.st_size <= bound
            and (mode is None or stat.S_IMODE(before.st_mode) == mode),
            "root_file",
        )
        raw = stream.read(bound + 1)
        after = os.fstat(stream.fileno())
        need(
            len(raw) <= bound
            and (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "file_changed",
        )
        return raw


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw, *, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), OWNER, 0)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path.parent)


def mkdir(path, mode):
    path.mkdir(mode=mode)
    path.chmod(mode)
    if os.geteuid() == 0:
        os.chown(path, OWNER, 0)
    fsync_dir(path.parent)


def command(args, *, input_data=None, pass_fds=(), timeout=30):
    result = subprocess.run(
        args,
        input=input_data,
        capture_output=True,
        timeout=timeout,
        pass_fds=pass_fds,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    need(result.returncode == 0, "host_command_failed")
    return result.stdout


def check_read_token(token, access):
    need(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", token), "read_token")
    need(
        isinstance(access, dict)
        and access.get("version") == 1
        and isinstance(access.get("principals"), list),
        "live_access_policy",
    )
    rows = [
        r
        for r in access["principals"]
        if isinstance(r, dict) and r.get("token_sha256") == digest(token.encode("ascii"))
    ]
    need(
        len(rows) == 1
        and rows[0].get("project_id") is None
        and isinstance(rows[0].get("scopes"), list)
        and sorted(rows[0]["scopes"]) == ["plan", "read"],
        "read_only_principal",
    )


def context(user):
    return f"user={user},host=hp-ubuntu2604-server,addr=127.0.0.1,laddr=192.168.68.67,lport=22"


def effective(user, *, config=None, pass_fds=()):
    args = ["/usr/sbin/sshd", "-T"]
    if config:
        args += ["-f", config]
    raw = command(args + ["-C", context(user)], pass_fds=pass_fds)
    fields = {}
    for line in raw.decode("utf-8").splitlines():
        name, _, value = line.partition(" ")
        if name not in fields:
            fields[name] = value
        else:
            prior = fields[name]
            fields[name] = (*prior, value) if isinstance(prior, tuple) else (prior, value)
    return fields


def expected_effective(user):
    need(user in {DEPLOY, CLIENT}, "fixed_identity")
    return {
        "authenticationmethods": "publickey",
        "pubkeyauthentication": "yes",
        "usepam": "yes",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "authorizedkeysfile": str(POLICY_DIR / "keys" / user),
        "forcecommand": str(FORCED) if user == DEPLOY else "/usr/bin/false",
        "disableforwarding": "yes" if user == DEPLOY else "no",
        "allowtcpforwarding": "no" if user == DEPLOY else "local",
        "allowstreamlocalforwarding": "no",
        "allowagentforwarding": "no",
        "permittunnel": "no",
        "x11forwarding": "no",
        "permittty": "no",
        "permituserrc": "no",
        "permitopen": "none" if user == DEPLOY else "127.0.0.1:18588",
        "permitlisten": "none",
        "maxsessions": "1" if user == DEPLOY else "0",
        "allowusers": user,
        "allowgroups": user,
    }


def check_effective(user, fields):
    expected = expected_effective(user)
    need(
        all(fields.get(key) == value for key, value in expected.items()), "sshd_capability_mismatch"
    )


def candidate_main(raw, included, snippet_path):
    lines = raw.decode("utf-8").splitlines(keepends=True)
    pattern = re.compile(
        r"\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf\s*(?:#.*)?$", re.IGNORECASE
    )
    positions = [i for i, line in enumerate(lines) if pattern.fullmatch(line.rstrip("\n"))]
    need(len(positions) == 1 and len(included) <= 64, "sshd_include_shape")
    rows = [(p.name, str(p)) for p in included] + [(SSH_SNIPPET.name, snippet_path)]
    need(len({name for name, _ in rows}) == len(rows), "sshd_include_duplicate")
    lines[positions[0]] = "".join("Include " + path + "\n" for _, path in sorted(rows))
    return "".join(lines).encode()


def validate_candidate_ssh(main_raw, included, old):
    snippet = os.memfd_create("quota-ssh-snippet", os.MFD_CLOEXEC)
    candidate = os.memfd_create("quota-ssh-candidate", os.MFD_CLOEXEC)
    sudoers = os.memfd_create("quota-sudoers-candidate", os.MFD_CLOEXEC)
    try:
        os.write(snippet, SSH_TEXT.encode())
        os.lseek(snippet, 0, os.SEEK_SET)
        # sshd closes inherited FDs before opening its configuration. The
        # still-running root parent's descriptors remain available to the child.
        main = candidate_main(main_raw, included, f"/proc/{os.getpid()}/fd/{snippet}")
        os.write(candidate, main)
        os.lseek(candidate, 0, os.SEEK_SET)
        os.write(sudoers, SUDOERS_TEXT.encode())
        os.lseek(sudoers, 0, os.SEEK_SET)
        descriptors = (snippet, candidate)
        config = f"/proc/{os.getpid()}/fd/{candidate}"
        command(["/usr/sbin/sshd", "-t", "-f", config], pass_fds=descriptors)
        for user in (DEPLOY, CLIENT):
            check_effective(user, effective(user, config=config, pass_fds=descriptors))
        for user in old:
            need(
                effective(user, config=config, pass_fds=descriptors) == old[user],
                "existing_ssh_policy_changed",
            )
        command(
            ["/usr/sbin/visudo", "-cf", f"/proc/{os.getpid()}/fd/{sudoers}"], pass_fds=(sudoers,)
        )
    finally:
        for fd in (snippet, candidate, sudoers):
            os.close(fd)


def preflight(value, token):
    for path in (
        ENTRY,
        FORCED,
        ROOT_ENTRY,
        POLICY_DIR,
        JOURNAL,
        LOCK,
        RECEIPT,
        READ_TOKEN,
        SSH_SNIPPET,
        SUDOERS,
    ):
        need(not os.path.lexists(path), "existing_access_requires_reconcile")
        root_directory(path.parent)
    for name in (DEPLOY, CLIENT):
        try:
            pwd.getpwnam(name)
        except KeyError:
            pass
        else:
            raise Refused("existing_identity_requires_reconcile")
        try:
            grp.getgrnam(name)
        except KeyError:
            pass
        else:
            raise Refused("existing_identity_requires_reconcile")
    need(
        digest(read(RUNTIME, 1024**2)) == value["runtime_launcher_sha256"]
        and digest(read(UNIT, 65536)) == value["unit_sha256"],
        "runtime_pin_drift",
    )
    current = BASE / "current"
    info = current.lstat()
    need(stat.S_ISLNK(info.st_mode) and info.st_uid == OWNER, "runtime_current")
    target = os.readlink(current)
    need(re.fullmatch(r"releases/[a-z0-9][a-z0-9_-]{0,95}", target), "runtime_current")
    check_read_token(token, parse(read(BASE / target / "config/access.json")))
    command(["/usr/sbin/sshd", "-t"])
    command(["/usr/sbin/visudo", "-c"])
    existing_users = {account.pw_name for account in pwd.getpwall()} - {DEPLOY, CLIENT}
    need(
        {"morris", "quota-deploy"} <= existing_users and len(existing_users) <= 256,
        "existing_identity_inventory",
    )
    old = {user: effective(user) for user in sorted(existing_users)}
    main_raw = read(SSH_CONFIG)
    included = sorted(SSH_SNIPPET.parent.glob("*.conf"))
    included_hashes = {str(path): digest(read(path)) for path in included}
    validate_candidate_ssh(main_raw, included, old)
    need(
        read(SSH_CONFIG) == main_raw
        and all(digest(read(Path(path))) == sha for path, sha in included_hashes.items()),
        "ssh_baseline_drift",
    )
    return {
        "old": old,
        "main_raw": main_raw,
        "included": included,
        "included_hashes": included_hashes,
    }


def save(record, *, new=False):
    if new:
        write_new(RECEIPT, canonical(record))
    else:
        temporary = RECEIPT.with_suffix(".pending")
        write_new(temporary, canonical(record))
        os.replace(temporary, RECEIPT)
        fsync_dir(RECEIPT.parent)


def phase(record, name):
    record["phase"] = name
    save(record)


def key_lines(value):
    return {
        DEPLOY: ("restrict " + value["deploy_public_key"] + "\n").encode(),
        CLIENT: (
            'restrict,port-forwarding,permitopen="127.0.0.1:18588" '
            + value["client_public_key"]
            + "\n"
        ).encode(),
    }


def quarantine(path, expected, operation):
    if not os.path.lexists(path):
        return
    need(read(path, 1024**2) == expected, "foreign_access_file")
    target = path.with_name(path.name + ".disabled-" + operation)
    need(not os.path.lexists(target), "quarantine_exists")
    os.rename(path, target)
    fsync_dir(path.parent)


def installed_ssh(old):
    command(["/usr/sbin/sshd", "-t"])
    for user in (DEPLOY, CLIENT):
        check_effective(user, effective(user))
    for user in old:
        need(effective(user) == old[user], "existing_ssh_policy_changed")


def apply(value, proof, token):
    snapshot = preflight(value, token)
    record = {
        "schema": 1,
        "operation_id": value["operation_id"],
        "status": "claimed",
        "phase": "pre_mutation",
        "provider_calls": 0,
        "automatic_retry": False,
        "payload_sha256": proof["payload_sha256"],
    }
    save(record, new=True)
    created = []
    try:
        phase(record, "install_maintenance")
        mkdir(POLICY_DIR, 0o755)
        mkdir(POLICY_DIR / "keys", 0o755)
        mkdir(JOURNAL, 0o700)
        mkdir(JOURNAL / "history", 0o700)
        write_new(LOCK, b"")
        write_new(ENTRY, proof["helper"], mode=0o644)
        write_new(FORCED, FORCED_TEXT.encode(), mode=0o755)
        write_new(ROOT_ENTRY, ROOT_TEXT.encode(), mode=0o755)
        policy = {
            "schema": "hp-quota-shared-maintenance-policy-v1",
            "helper_sha256": proof["helper_sha256"],
            "launcher_sha256": RUNTIME_PIN,
            "unit_sha256": UNIT_PIN,
            "service_user": "quota-shared-broker",
            "service_group": "quota-shared-broker",
        }
        write_new(POLICY_DIR / "policy.json", canonical(policy))
        encrypted = command(
            ["/usr/bin/systemd-creds", "encrypt", "--with-key=host", "--name=read-token", "-", "-"],
            input_data=token.encode(),
        )
        need(0 < len(encrypted) <= 65536, "encrypted_credential_bound")
        write_new(READ_TOKEN, encrypted)
        recovered = command(
            ["/usr/bin/systemd-creds", "decrypt", "--name=read-token", str(READ_TOKEN), "-"]
        )
        need(recovered == token.encode(), "credential_roundtrip")
        status = parse(
            command(
                [str(ROOT_ENTRY)],
                input_data=canonical(
                    {"schema": "hp-quota-shared-maintenance-v1", "action": "status"}
                ),
            )
        )
        need(isinstance(status, dict) and status.get("status") == "ready", "maintenance_not_ready")
        phase(record, "create_expired_identities")
        for name in (DEPLOY, CLIENT):
            command(
                [
                    "/usr/sbin/useradd",
                    "--system",
                    "--user-group",
                    "--home-dir",
                    "/nonexistent",
                    "--no-create-home",
                    "--shell",
                    "/bin/sh",
                    "--expiredate",
                    "1",
                    name,
                ]
            )
            created.append(name)
        for name, raw in key_lines(value).items():
            write_new(POLICY_DIR / "keys" / name, raw, mode=0o644)
        write_new(SUDOERS, SUDOERS_TEXT.encode(), mode=0o440)
        command(["/usr/sbin/visudo", "-c"])
        phase(record, "activate_scoped_ssh")
        # Refuse a concurrent SSH config change rather than guessing a merge.
        need(
            read(SSH_CONFIG) == snapshot["main_raw"]
            and sorted(SSH_SNIPPET.parent.glob("*.conf")) == snapshot["included"]
            and all(
                digest(read(Path(path))) == sha for path, sha in snapshot["included_hashes"].items()
            ),
            "ssh_baseline_drift",
        )
        write_new(SSH_SNIPPET, SSH_TEXT.encode(), mode=0o644)
        installed_ssh(snapshot["old"])
        phase(record, "reload_ssh")
        command(["/usr/bin/systemctl", "reload", "ssh.service"])
        installed_ssh(snapshot["old"])
        phase(record, "enable_verified_identities")
        for name in (DEPLOY, CLIENT):
            command(["/usr/sbin/usermod", "--expiredate", "", name])
        record.update(status="complete", phase="complete", existing_users_unchanged=True)
        save(record)
        return record
    except Exception as exc:  # noqa: BLE001 - exact fixed cleanup; never reflect private errors
        record["error_code"] = str(exc) if isinstance(exc, Refused) else "host_exception"
        record["failure_phase"] = record["phase"]
        failures = []
        for name in created:
            try:
                command(["/usr/sbin/usermod", "--expiredate", "1", name])
            except Exception:  # noqa: BLE001 - keep uncertain disablement in the original receipt
                failures.append("account_disable")
        for path, raw in [(SSH_SNIPPET, SSH_TEXT.encode()), (SUDOERS, SUDOERS_TEXT.encode())] + [
            (POLICY_DIR / "keys" / name, raw) for name, raw in key_lines(value).items()
        ]:
            try:
                quarantine(path, raw, value["operation_id"])
            except Exception:  # noqa: BLE001 - foreign/drifting files are retained, never overwritten
                failures.append("file_quarantine")
        # No blind reload after an uncertain reload. Existing users were proven
        # unchanged before the only reload; disabled new keys cannot authenticate.
        record.update(status="needs_reconcile", phase="disabled", cleanup_uncertain=bool(failures))
        save(record)
        return record


def read_status():
    if not RECEIPT.exists():
        return {"status": "not_started", "automatic_retry": False}
    return parse(read(RECEIPT, 65536, mode=0o600))


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.alarm(240)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--validate", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--status", action="store_true")
    parser.add_argument("--payload-sha256")
    parser.add_argument("--helper-sha256")
    args = parser.parse_args()
    host = os.geteuid() == 0 and socket.gethostname() == "hp-ubuntu2604-server"
    if args.status:
        need(host, "fixed_hp_root")
        return read_status()
    envelope = parse(sys.stdin.buffer.read(MAX_INPUT + 1))
    need(
        isinstance(envelope, dict) and set(envelope) == {"payload", "read_token"}, "envelope_schema"
    )
    value = envelope["payload"]
    proof = validate_payload(value, args.payload_sha256, args.helper_sha256)
    token = envelope["read_token"]
    need(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", token), "read_token")
    if args.validate:
        if host:
            preflight(value, token)
        return {
            "status": "validated",
            "operation_id": value["operation_id"],
            "host_preflight": host,
            "provider_calls": 0,
        }
    need(host, "fixed_hp_root")
    root_directory(GATE_LOCK.parent)
    fd = os.open(GATE_LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as lock:
        info = os.fstat(lock.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == OWNER
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "gate_lock",
        )
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return apply(value, proof, token)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - fixed output contains no input values or stderr
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "code": str(exc) if isinstance(exc, Refused) else "host_exception",
                    "reason": "inspect_original_root_receipt",
                    "automatic_retry": False,
                    "provider_calls": 0,
                }
            )
        )
        raise SystemExit(1) from None
