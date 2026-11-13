"""Reviewed one-operation HP encrypted API gate; stdlib only in the root process.

The bundle is pinned by the operator. Existing configuration, ledger and runtime
credentials remain bound to their current instance. Application code runs only
as the existing unprivileged service user. No provider requests or model jobs.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import pwd
import resource
import secrets
import socket
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

OPERATION = "quota-encrypted-20261114-v1"
EXPECTED_RELEASE = "shared-broker-20261113-api-v2"
EXPECTED_MANIFEST = "f6975d83a5614bdfedf9b15b5d99d5a34de1bdc7d633b42a34c7e2f2e790ee5a"
EXPECTED_CADDY = "75c2509d1d1c6b4d9a0cce8821aa87eb027fdb0649f7c7c2355c6f66d4260d00"
EXPECTED_UNIT = "b46d207b265a3f5e20e76c5b13a8fc55c33bd1a5aaa19623afe91deee0e96467"
IDENTITY = "quota-shared-broker"
BASE = Path("/opt/quota-private-crypto")
STATE = Path("/var/lib/quota-private-crypto")
CONFIG = Path("/etc/quota-shared-broker")
BACKUP = Path("/var/backups/quota-shared-broker") / OPERATION
ENTRY = Path("/usr/local/libexec/hp-quota-shared-maintenance.py")
LAUNCHER = Path("/usr/local/libexec/quota-shared-runtime.py")
POLICY = Path("/etc/quota-shared-maintenance/policy.json")
UNIT = Path("/etc/systemd/system/quota-shared-broker.service")
CADDY = Path("/etc/caddy/Caddyfile")
FIXED_FILES = {
    "package.json",
    "site.tar.gz",
    "hp_shared_maintenance.py",
    "hp_shared_runtime.py",
    "hp_private_crypto.py",
    "hp_quota_encrypted_api.caddy",
    "worker.crt",
    "tunnel.pub",
}
TUNNEL_CONFIG = Path("/etc/ssh/sshd_config.d/74-quota-local-tunnel.conf")
TUNNEL_HOME = Path("/var/lib/quota-local-tunnel")


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def command(argv, *, data=None, timeout=60):
    result = subprocess.run(
        argv,
        input=data,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    need(result.returncode == 0, "command_failed:" + Path(argv[0]).name)
    return result.stdout


def atomic(path, raw, *, mode=0o600, uid=0, gid=0, new=False):
    need(not path.is_symlink(), "destination_symlink")
    if new:
        need(not path.exists(), "destination_exists")
    fd, temporary = tempfile.mkstemp(prefix=".quota-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if new:
            os.link(temporary, path, follow_symlinks=False)
            Path(temporary).unlink()
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def bundle_files(directory, expected):
    lock = (directory / "bundle.json").read_bytes()
    need(sha(lock) == expected, "bundle_digest")
    record = json.loads(lock)
    need(set(record) == FIXED_FILES, "bundle_scope")
    files = {}
    for name, digest in record.items():
        path = directory / name
        info = path.lstat()
        need(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 32 * 1024**2,
            "bundle_file_metadata",
        )
        raw = path.read_bytes()
        need(len(raw) <= 32 * 1024**2 and sha(raw) == digest, "bundle_file_drift")
        files[name] = raw
    return files


def encrypted_unit(original):
    need(sha(original) == EXPECTED_UNIT, "unit_compare_and_swap")
    return original.replace(
        b"After=network-online.target\n",
        b"After=network-online.target quota-private-publish.service\n"
        b"Wants=quota-private-delegate.service quota-private-publish.service\n",
        1,
    ).replace(
        b"LoadCredentialEncrypted=runtime.json:/etc/credstore.encrypted/quota-shared-broker-runtime\n",
        b"LoadCredentialEncrypted=runtime.json:/etc/credstore.encrypted/quota-shared-broker-runtime\n"
        b"LoadCredentialEncrypted=worker.token:/etc/credstore.encrypted/quota-private-worker\n",
        1,
    )


def caddy_candidate(original, fragment):
    need(
        sha(original) == EXPECTED_CADDY and b"@quota_encrypted_config" not in original,
        "caddy_compare_and_swap",
    )
    anchor = b"\t        @quota_project_get {"
    need(original.count(anchor) == 1, "caddy_unique_anchor")
    inserted = b"".join(b"\t        " + line for line in fragment.splitlines(keepends=True))
    candidate = original.replace(anchor, inserted + anchor, 1)
    need(candidate.replace(inserted, b"", 1) == original, "only_quota_routes")
    return candidate


def maintenance(action, **kwargs):
    wire = {"schema": "hp-quota-shared-maintenance-v1", "action": action, **kwargs}
    raw = command(
        ["/usr/bin/python3", "-I", "-B", str(ENTRY)], data=json.dumps(wire).encode(), timeout=240
    )
    value = json.loads(raw)
    need(isinstance(value, dict), "maintenance_response")
    return value


def crypto_unit(action):
    key = "root.pem" if action == "delegate" else "signer.pem"
    extra = "" if action == "delegate" else ("After=quota-private-delegate.service\n")
    credentials = f"LoadCredentialEncrypted={key}:/etc/credstore.encrypted/quota-private-{key}\n"
    if action == "publish":
        credentials += "LoadCredentialEncrypted=storage.key:/etc/credstore.encrypted/quota-private-storage.key\n"
    return (
        f"[Unit]\nDescription=Quota private API {action}\n{extra}\n[Service]\n"
        f"Type=oneshot\nUser={IDENTITY}\nGroup={IDENTITY}\nUMask=0077\n"
        "StateDirectory=quota-private-crypto\nStateDirectoryMode=0700\n"
        "RuntimeDirectory=quota-private-crypto\nRuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n"
        f"ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/quota-private-crypto.py {action}\n"
        f"{credentials}TimeoutStartSec=30\nMemoryMax=128M\nLimitCORE=0\nNoNewPrivileges=yes\n"
        "ProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nPrivateDevices=yes\n"
        "ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n"
        "CapabilityBoundingSet=\nRestrictAddressFamilies=AF_UNIX\nIPAddressDeny=any\n"
    ).encode()


def create_owned(path, raw, created, **kwargs):
    atomic(path, raw, new=True, **kwargs)
    created[str(path)] = sha(raw)


def install_tunnel(public, created, identity):
    need(not TUNNEL_CONFIG.exists() and not TUNNEL_HOME.exists(), "tunnel_setup_exists")
    try:
        pwd.getpwnam("quota-local-tunnel")
    except KeyError:
        pass
    else:
        raise ValueError("tunnel_identity_exists")
    command(
        [
            "/usr/sbin/useradd",
            "--system",
            "--user-group",
            "--no-create-home",
            "--home-dir",
            str(TUNNEL_HOME),
            "--shell",
            "/bin/sh",
            "--expiredate",
            "1",
            "quota-local-tunnel",
        ]
    )
    identity["uid"] = pwd.getpwnam("quota-local-tunnel").pw_uid
    TUNNEL_HOME.mkdir(mode=0o755)
    TUNNEL_HOME.chmod(0o755)
    ssh = TUNNEL_HOME / ".ssh"
    ssh.mkdir(mode=0o755)
    ssh.chmod(0o755)
    key = b'restrict,port-forwarding,permitlisten="127.0.0.1:18443" ' + public.strip() + b"\n"
    create_owned(ssh / "authorized_keys", key, created, mode=0o644)
    text = (
        "Match User quota-local-tunnel\n"
        "    AuthenticationMethods publickey\n    PasswordAuthentication no\n"
        "    PubkeyAuthentication yes\n    DisableForwarding no\n    AllowUsers quota-local-tunnel\n"
        "    KbdInteractiveAuthentication no\n    AllowTcpForwarding remote\n"
        "    PermitListen 127.0.0.1:18443\n    PermitOpen none\n"
        "    GatewayPorts no\n    AllowStreamLocalForwarding no\n"
        "    X11Forwarding no\n    AllowAgentForwarding no\n    PermitTTY no\n"
        "    PermitTunnel no\n    PermitUserRC no\n    MaxSessions 0\n"
        "    ForceCommand /usr/sbin/nologin\nMatch all\n"
    )
    create_owned(TUNNEL_CONFIG, text.encode(), created, mode=0o644)
    command(["/usr/sbin/sshd", "-t"])
    effective = command(
        ["/usr/sbin/sshd", "-T", "-C", "user=quota-local-tunnel,host=localhost,addr=127.0.0.1"]
    ).decode()
    options = dict(line.split(" ", 1) for line in effective.splitlines() if " " in line)
    for name, value in {
        "allowtcpforwarding": "remote",
        "permitlisten": "127.0.0.1:18443",
        "permitopen": "none",
        "forcecommand": "/usr/sbin/nologin",
        "gatewayports": "no",
        "permittty": "no",
        "passwordauthentication": "no",
        "maxsessions": "0",
        "usepam": "yes",
    }.items():
        need(options.get(name) == value, "tunnel_effective_policy")
    command(["/usr/bin/systemctl", "reload", "ssh"])
    command(["/usr/sbin/usermod", "--expiredate", "", "quota-local-tunnel"])


def cleanup_created(created, tunnel_uid):
    for action in ("publish", "delegate"):
        path = Path(f"/etc/systemd/system/quota-private-{action}.timer")
        if str(path) in created:
            need(sha(path.read_bytes()) == created[str(path)], "created_timer_drift")
            command(["/usr/bin/systemctl", "disable", "--now", path.name])
    if tunnel_uid is not None:
        need(
            pwd.getpwnam("quota-local-tunnel").pw_uid == tunnel_uid, "created_tunnel_identity_drift"
        )
        command(["/usr/sbin/usermod", "--expiredate", "1", "quota-local-tunnel"])
    for path, name in (
        (TUNNEL_HOME / ".ssh/authorized_keys", "disabled-tunnel-authorized_keys"),
        (TUNNEL_CONFIG, "disabled-tunnel.conf"),
    ):
        if str(path) in created:
            need(sha(path.read_bytes()) == created[str(path)], "created_tunnel_file_drift")
            os.replace(path, BACKUP / name)
    if str(TUNNEL_CONFIG) in created:
        command(["/usr/sbin/sshd", "-t"])
        command(["/usr/bin/systemctl", "reload", "ssh"])


def restore_runtime(
    original, *, operation, publish_attempted, runtime_changed, caddy_changed, service_gid
):
    restart = False
    if publish_attempted:
        observed = maintenance("status", operation_id=operation)
        if observed.get("status") != "not_started":
            rollback = maintenance("rollback", operation_id=operation)
            need(rollback.get("status") == "rolled_back", "broker_rollback_requires_reconcile")
            restart = True
    restore = (
        [
            ("launcher", LAUNCHER, 0o750, service_gid),
            ("helper", ENTRY, 0o750, 0),
            ("policy", POLICY, 0o600, 0),
            ("unit", UNIT, 0o644, 0),
        ]
        if runtime_changed
        else []
    )
    if caddy_changed:
        restore.append(("caddy", CADDY, stat.S_IMODE(CADDY.stat().st_mode), CADDY.stat().st_gid))
    for name, path, mode, gid in restore:
        atomic(path, original[name], mode=mode, gid=gid)
    command(["/usr/bin/systemctl", "daemon-reload"])
    if restart:
        command(["/usr/bin/systemctl", "restart", "quota-shared-broker.service"], timeout=240)
    if caddy_changed:
        command(["/usr/bin/systemctl", "reload", "caddy"])


def install(bundle, digest):
    need(
        os.geteuid() == 0 and socket.gethostname() == "hp-ubuntu2604-server", "fixed_hp_root_entry"
    )
    files = bundle_files(bundle, digest)
    need(
        not BACKUP.exists() and not BASE.exists() and not STATE.exists() and not CONFIG.exists(),
        "prior_operation_requires_reconcile",
    )
    status = maintenance("status")
    need(
        status.get("status") == "ready"
        and status.get("current") == EXPECTED_RELEASE
        and status.get("manifest_sha256") == EXPECTED_MANIFEST,
        "release_compare_and_swap",
    )
    original = {
        name: path.read_bytes()
        for name, path in (
            ("launcher", LAUNCHER),
            ("helper", ENTRY),
            ("policy", POLICY),
            ("unit", UNIT),
            ("caddy", CADDY),
        )
    }
    policy = json.loads(original["policy"])
    need(
        sha(original["launcher"]) == policy["launcher_sha256"]
        and sha(original["helper"]) == policy["helper_sha256"],
        "existing_pin_drift",
    )
    next_unit = encrypted_unit(original["unit"])
    next_caddy = caddy_candidate(original["caddy"], files["hp_quota_encrypted_api.caddy"])
    account = pwd.getpwnam(IDENTITY)
    owner = pwd.getpwnam("morris")
    need(account.pw_uid != 0 and owner.pw_uid != 0, "service_identity")
    planned = [
        TUNNEL_CONFIG,
        TUNNEL_HOME,
        Path("/usr/local/libexec/quota-private-crypto.py"),
        Path(owner.pw_dir) / ".local/share/quota-private-api",
    ]
    planned += [
        Path("/etc/credstore.encrypted") / ("quota-private-" + name)
        for name in ("root.pem", "signer.pem", "storage.key", "worker")
    ]
    planned += [
        Path(f"/etc/systemd/system/quota-private-{action}.{kind}")
        for action in ("delegate", "publish")
        for kind in ("service", "timer")
    ]
    need(
        all(not p.exists() and not p.is_symlink() for p in planned),
        "existing_resource_requires_reconcile",
    )
    try:
        pwd.getpwnam("quota-local-tunnel")
    except KeyError:
        pass
    else:
        raise ValueError("existing_tunnel_identity")
    secret = Path(f"/run/user/{owner.pw_uid}/{OPERATION}/worker.token")
    info = secret.lstat()
    need(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == owner.pw_uid
        and stat.S_IMODE(info.st_mode) == 0o600
        and 32 <= info.st_size <= 4096,
        "worker_credential",
    )
    worker_token = secret.read_bytes().strip()
    need(
        worker_token.isascii() and not any(chr(c).isspace() for c in worker_token),
        "worker_credential",
    )
    need(
        b"ssh-ed25519 " == files["tunnel.pub"][:12] and len(files["tunnel.pub"]) < 256,
        "forward_key",
    )
    payload = json.loads(files["package.json"])
    need(
        payload["expected_current"] == EXPECTED_RELEASE
        and payload["expected_manifest_sha256"] == EXPECTED_MANIFEST,
        "payload_baseline",
    )
    BACKUP.mkdir(mode=0o700)
    for name, raw in original.items():
        atomic(BACKUP / name, raw, new=True)
    atomic(BACKUP / "candidate.Caddyfile", next_caddy, new=True)
    command(
        [
            "/usr/bin/caddy",
            "validate",
            "--config",
            str(BACKUP / "candidate.Caddyfile"),
            "--adapter",
            "caddyfile",
        ]
    )
    receipt = {
        "operation": OPERATION,
        "phase": "prepared",
        "bundle_sha256": digest,
        "broker_operation": payload["operation_id"],
        "previous": EXPECTED_RELEASE,
    }
    atomic(BACKUP / "receipt.json", json.dumps(receipt).encode(), new=True)

    def phase(name):
        receipt["phase"] = name
        atomic(BACKUP / "receipt.json", json.dumps(receipt).encode())

    publish_attempted = runtime_changed = caddy_changed = False
    created, tunnel_identity = {}, {}
    try:
        phase("install_fixed_runtime")
        BASE.mkdir(mode=0o755)
        BASE.chmod(0o755)
        with tarfile.open(fileobj=io.BytesIO(files["site.tar.gz"]), mode="r:gz") as archive:
            members = archive.getmembers()
            need(
                len(members) <= 3000 and sum(m.size for m in members) <= 100 * 1024**2, "site_bound"
            )
            for item in members:
                path = Path(item.name)
                need(
                    item.isfile()
                    and path.parts[0] == "site"
                    and not path.is_absolute()
                    and ".." not in path.parts,
                    "site_scope",
                )
                target = BASE / path
                target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
                for parent in target.parents:
                    if parent == BASE:
                        break
                    parent.chmod(0o755)
                atomic(target, archive.extractfile(item).read(), mode=0o644, new=True)
        # Fixed crypto code has its own immutable snapshot, independent of future app publishes.
        for row in payload["files"]:
            if row["path"].startswith("app/quota_broker/"):
                path = Path(row["path"])
                need(not path.is_absolute() and ".." not in path.parts, "app_scope")
                target = BASE / path
                target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
                for parent in target.parents:
                    if parent == BASE:
                        break
                    parent.chmod(0o755)
                raw = base64.b64decode(row["content_base64"], validate=True)
                need(sha(raw) == row["sha256"], "app_digest")
                atomic(target, raw, mode=0o644, new=True)
        STATE.mkdir(mode=0o700)
        os.chown(STATE, account.pw_uid, account.pw_gid)
        CONFIG.mkdir(mode=0o750)
        CONFIG.chmod(0o750)
        os.chown(CONFIG, 0, account.pw_gid)
        phase("initialize_host_credentials")
        with tempfile.TemporaryDirectory(prefix="quota-key-init-", dir="/run") as temporary:
            directory = Path(temporary)
            os.chown(directory, account.pw_uid, account.pw_gid)
            # Root launches the reviewed app only as the unprivileged service UID.
            code = """import sys,json,base64,os
from pathlib import Path
sys.path[:0]=['/opt/quota-private-crypto/app','/opt/quota-private-crypto/site']
from quota_broker.crypto_admin import main
d=Path(sys.argv[1]);s=Path('/var/lib/quota-private-crypto')
os.umask(0o077)
for n in ('root','signer'):
 assert main(['keygen','--private',str(d/(n+'.pem')),'--public',str(s/(n+'.pub'))])==0
(d/'storage.key').write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
(s/'profile.json').write_text(json.dumps({'audience':'https://momonong.me/quota/api/v1','epoch':1,'roots':{'hp-root-1':(s/'root.pub').read_text().strip()},'minimum_version':1}))
"""
            command(
                [
                    "/usr/sbin/runuser",
                    "-u",
                    IDENTITY,
                    "--",
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(directory),
                ]
            )
            for name in ("root.pem", "signer.pem", "storage.key"):
                output = Path("/etc/credstore.encrypted") / ("quota-private-" + name)
                need(not output.exists(), "credential_exists")
                command(
                    [
                        "/usr/bin/systemd-creds",
                        "encrypt",
                        "--with-key=host",
                        "--name=" + name,
                        str(directory / name),
                        str(output),
                    ]
                )
        command(
            [
                "/usr/bin/systemd-creds",
                "encrypt",
                "--with-key=host",
                "--name=worker.token",
                "-",
                "/etc/credstore.encrypted/quota-private-worker",
            ],
            data=worker_token,
        )
        secret.unlink()
        token = secrets.token_urlsafe(48)
        access = {
            "version": 1,
            "principals": [
                {
                    "token_sha256": sha(token.encode()),
                    "project_id": "private-development",
                    "scopes": ["read", "plan", "execute"],
                    "allowed_execution": ["local_only"],
                    "default_execution": "local_only",
                    "encryption_required": True,
                    "limits": {
                        "max_input_bytes": 2097152,
                        "max_output_tokens": 1024,
                        "max_inflight": 2,
                        "max_deadline_seconds": 900,
                    },
                }
            ],
        }
        encrypted = {
            "profile": str(STATE / "profile.json"),
            "manifest": str(STATE / "manifest.json"),
            "recipients": "/run/quota-private-crypto/recipients.json",
            "state": "/var/lib/quota-shared-broker/crypto-trust.sqlite",
            "replay": "/var/lib/quota-shared-broker/crypto-replay.sqlite",
        }
        local = {
            "version": 1,
            "target": {
                "target_id": "rtx4090",
                "base_url": "https://127.0.0.1:18443",
                "deployment_id": "dep_26c3f5dd99cffd9cd32d6dc62212190a0e14664484547e2e1867b274eb52465d",
                "model": "Qwen/Qwen3.5-4B",
            },
            "token_file": "/run/quota-shared-broker/worker.token",
            "ca_file": str(CONFIG / "worker.crt"),
            "exact_lookup": True,
        }
        private = {
            "encrypted": str(CONFIG / "encrypted.json"),
            "local": str(CONFIG / "local.json"),
            "access": str(CONFIG / "local-access.json"),
            "worker_credential": "/run/credentials/quota-shared-broker.service/worker.token",
        }
        for name, value in (
            ("encrypted.json", encrypted),
            ("local.json", local),
            ("local-access.json", access),
            ("private-api.json", private),
        ):
            atomic(
                CONFIG / name, json.dumps(value).encode(), mode=0o640, gid=account.pw_gid, new=True
            )
        atomic(CONFIG / "worker.crt", files["worker.crt"], mode=0o644, new=True)
        consumer = Path(owner.pw_dir) / ".local/share/quota-private-api"
        need(not consumer.exists(), "consumer_configuration_exists")
        consumer.mkdir(mode=0o700, parents=True)
        os.chown(consumer, owner.pw_uid, owner.pw_gid)
        atomic(
            consumer / "project.token", token.encode(), uid=owner.pw_uid, gid=owner.pw_gid, new=True
        )
        atomic(
            consumer / "profile.json",
            (STATE / "profile.json").read_bytes(),
            uid=owner.pw_uid,
            gid=owner.pw_gid,
            new=True,
        )
        atomic(
            Path("/usr/local/libexec/quota-private-crypto.py"),
            files["hp_private_crypto.py"],
            mode=0o755,
            new=True,
        )
        for action, interval in (("delegate", "7d"), ("publish", "5min")):
            create_owned(
                Path(f"/etc/systemd/system/quota-private-{action}.service"),
                crypto_unit(action),
                created,
                mode=0o644,
            )
            timer = (
                "[Unit]\nDescription=Quota private key maintenance\n[Timer]\nOnBootSec=30s\n"
                f"OnUnitActiveSec={interval}\nAccuracySec=1s\n[Install]\nWantedBy=timers.target\n"
            )
            create_owned(
                Path(f"/etc/systemd/system/quota-private-{action}.timer"),
                timer.encode(),
                created,
                mode=0o644,
            )
        command(
            [
                "/usr/bin/systemd-analyze",
                "verify",
                "/etc/systemd/system/quota-private-delegate.service",
                "/etc/systemd/system/quota-private-publish.service",
                "/etc/systemd/system/quota-private-delegate.timer",
                "/etc/systemd/system/quota-private-publish.timer",
            ]
        )
        command(["/usr/bin/systemctl", "daemon-reload"])
        phase("start_key_maintenance")
        command(["/usr/bin/systemctl", "start", "quota-private-delegate.service"])
        command(["/usr/bin/systemctl", "start", "quota-private-publish.service"])
        phase("install_restricted_tunnel")
        install_tunnel(files["tunnel.pub"], created, tunnel_identity)
        # Update only the three reviewed runtime pins; retain original SSH identities/policies.
        runtime_changed = True
        atomic(LAUNCHER, files["hp_shared_runtime.py"], mode=0o750, gid=account.pw_gid)
        atomic(ENTRY, files["hp_shared_maintenance.py"], mode=0o750)
        atomic(UNIT, next_unit, mode=0o644)
        policy.update(
            launcher_sha256=sha(files["hp_shared_runtime.py"]),
            helper_sha256=sha(files["hp_shared_maintenance.py"]),
            unit_sha256=sha(next_unit),
        )
        atomic(POLICY, json.dumps(policy).encode())
        command(["/usr/bin/systemctl", "daemon-reload"])
        phase("publish_broker")
        publish_attempted = True
        result = maintenance("publish", payload=payload)
        need(result.get("status") == "complete", "broker_publish_requires_reconcile")
        phase("reload_quota_ingress")
        need(sha(CADDY.read_bytes()) == EXPECTED_CADDY, "caddy_changed_during_install")
        caddy_changed = True
        atomic(CADDY, next_caddy, mode=stat.S_IMODE(CADDY.stat().st_mode), gid=CADDY.stat().st_gid)
        command(["/usr/bin/systemctl", "reload", "caddy"])
        command(
            [
                "/usr/bin/systemctl",
                "enable",
                "--now",
                "quota-private-delegate.timer",
                "quota-private-publish.timer",
            ]
        )
        receipt.update(
            phase="complete", release=payload["release_id"], caddy_sha256=sha(next_caddy)
        )
        atomic(BACKUP / "receipt.json", json.dumps(receipt).encode())
        print(json.dumps(receipt, sort_keys=True))
    except Exception as error:
        # Preserve new encrypted keys/receipts; never restore or replace the live ledger.
        receipt["failure_phase"] = receipt["phase"]
        receipt["error_type"] = type(error).__name__
        phase("reconcile_required")
        cleanup_created(created, tunnel_identity.get("uid"))
        private_path = CONFIG / "private-api.json"
        if private_path.exists():
            os.replace(private_path, BACKUP / "failed-private-api.json")
        restore_runtime(
            original,
            operation=payload["operation_id"],
            publish_attempted=publish_attempted,
            runtime_changed=runtime_changed,
            caddy_changed=caddy_changed,
            service_gid=account.pw_gid,
        )
        receipt.update(phase="rolled_back")
        atomic(BACKUP / "receipt.json", json.dumps(receipt).encode())
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha256", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        install(args.bundle, args.bundle_sha256)
    except Exception:  # noqa: BLE001 - redact root-gate failures; preserve original receipt
        print('{"status":"stopped","reason":"inspect_original_encrypted_operation"}')
        raise SystemExit(1) from None
