"""Fixed /quota deployment source, assembled into two standalone reviewed root gates.

Only stdlib runs as root. Existing source/runtime verifier scripts are copied from
the sealed source; application imports and smoke run under the new service user.
This file has no executable role selector. build.py appends one fixed entry point.
"""

import argparse
import base64
import fcntl
import getpass
import grp
import hashlib
import http.client
import io
import json
import os
import pwd
import re
import resource
import secrets
import socket
import stat
import subprocess
import sys
import tarfile
import time
import types
import uuid
import warnings
from pathlib import Path

MAX_BYTES = 192 * 1024 * 1024
HASH = re.compile(r"[a-f0-9]{64}")
HOSTS = {"asus": "asus-ubuntu2604-server", "hp": "hp-ubuntu2604-server"}
CADDY_SHA = "1262d38284cf1d6b3d616034290daf16198a822a58eef4852cdb7cbcfed6af02"
BROKER_CURRENT = "/opt/api-quota-broker/releases/release-d7fccdccbd40e6b9c3d25746bf46b2b1ff1b605b3d37f13d7beec7409a655afa"
HP_HOME_CURRENT = "/opt/momonong-home/releases/home-portal-c31097074c7af97635d3"
HP_HOME_CORE_SHA = "453522115be680fd866780d72a2a1f9491666462d8a0ac73bba19a1ee580ba15"
JOURNAL = Path("/var/lib/quota-web-deploy")
APP = Path("/opt/api-quota-dashboard")
CONFIG = Path("/etc/api-quota-dashboard")
FORWARD_HOME = Path("/var/lib/quota-forward")
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEibsvyokBkT8Lc8rF/nkhlvIFhvX6Yr8Xzh1hSgWGyv"
SSH_SCOPE_PATH = Path("/etc/ssh/sshd_config.d/quota-forward.conf")
SSH_SCOPE = b"""# Only the new quota forwarding identity.
Match User quota-forward
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:18586
    PermitListen none
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    ForceCommand /usr/bin/false
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthenticationMethods publickey
Match all
"""
ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}
DASHBOARD_UNIT = b"""[Unit]
Description=Protected read-only quota dashboard
After=network.target api-quota-broker-client.service

[Service]
Type=simple
User=api-quota-dashboard
Group=api-quota-dashboard
WorkingDirectory=/var/lib/api-quota-dashboard
StateDirectory=api-quota-dashboard
StateDirectoryMode=0700
UMask=0077
RuntimeDirectory=api-quota-dashboard
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=no
LoadCredentialEncrypted=read_plan_token:/etc/api-quota-dashboard/credentials/read_plan_token.cred
ExecStartPre=/usr/bin/install -m 0600 %d/read_plan_token /run/api-quota-dashboard/read_plan_token
ExecStart=/opt/api-quota-broker/current/runtime/bin/python -I -B -m quota_broker.cli dashboard-serve --reviewed-config /etc/api-quota-dashboard/metadata/reviewed.json --accounts-file /etc/api-quota-dashboard/metadata/accounts.json --activity-upstream http://127.0.0.1:18084 --activity-token-file /run/api-quota-dashboard/read_plan_token --dashboard-password-file /etc/api-quota-dashboard/browser-auth.json --public-base-url https://momonong.me/quota/ --port 18586
Restart=on-failure
RestartSec=5
MemoryMax=256M
MemorySwapMax=0
CPUQuota=50%
TasksMax=64
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_UNIX AF_INET
IPAddressDeny=any
IPAddressAllow=127.0.0.1/32
SocketBindDeny=any
SocketBindAllow=ipv4:tcp:18586

[Install]
WantedBy=multi-user.target
"""
TUNNEL_UNIT = b"""[Unit]
Description=Fixed encrypted quota forward to ASUS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=quota-forward
Group=quota-forward
UMask=0077
LoadCredentialEncrypted=forward_key:/etc/quota-forward/forward_key.cred
ExecStart=/usr/bin/ssh -F /dev/null -N -T -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none -o IdentityFile=%d/forward_key -o UserKnownHostsFile=/etc/quota-forward/known_hosts -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o RequestTTY=no -o ForwardAgent=no -o ClearAllForwardings=no -L 127.0.0.1:18095:127.0.0.1:18586 quota-forward@192.168.100.2
Restart=on-failure
RestartSec=5
MemoryMax=64M
MemorySwapMax=0
CPUQuota=10%
TasksMax=16
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_UNIX AF_INET
IPAddressDeny=any
IPAddressAllow=127.0.0.1/32
IPAddressAllow=192.168.100.2/32
SocketBindDeny=any
SocketBindAllow=ipv4:tcp:18095

[Install]
WantedBy=multi-user.target
"""
CADDY = b"""# Fixed /quota scope; Cloudflare supplies the original scheme.
@quota_plain {
    host momonong.me
    path /quota /quota/*
    not header X-Forwarded-Proto https
}
redir @quota_plain https://momonong.me{uri} 308
@quota_canonical {
    host momonong.me
    path /quota
}
redir @quota_canonical /quota/ 308
@quota_app {
    host momonong.me
    path /quota/*
}
handle @quota_app {
    route {
        @quota_public path /quota/ /quota/dashboard.css /quota/dashboard.js /quota/api/login /quota/api/logout /quota/api/snapshot /quota/api/usage /quota/api/advice
        handle @quota_public {
            header Cache-Control "no-store"
            request_header -X-Forwarded-*
            reverse_proxy 127.0.0.1:18095 {
                header_up Host momonong.me
                header_up -Authorization
                header_up -Proxy-Authorization
                header_up -Forwarded
                header_up -Remote-User
                header_up -Remote-Groups
                header_up -Remote-Name
                header_up -Remote-Email
                header_up -X-Remote-*
                header_up -X-Auth-*
                header_up -X-Original-*
                header_up -Cf-Access-*
                header_up X-Forwarded-Proto https
            }
        }
        respond 404
    }
}
"""


class GateError(ValueError):
    """Reason codes only: never reflect a path, metadata, command, or credential."""


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw: bytes) -> dict:
    def pairs(items: list) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise GateError("duplicate_json")
            result[key] = value
        return result

    def invalid(_: str) -> None:
        raise GateError("json_constant")

    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
        if not isinstance(result, dict):
            raise GateError("json_object")
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise GateError("json_invalid") from None


def safe_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.+/-]{1,180}", name)) and all(
        item not in {"", ".", ".."} for item in name.split("/")
    )


def inventory(role: str, files: dict[str, bytes]) -> None:
    if role == "hp":
        valid = set(files) == {"forward_key"}
    elif role == "asus":
        fixed = {
            "source.tar",
            "scripts/build_asus_release.py",
            "scripts/prepare_asus_runtime.py",
            "metadata/reviewed.json",
            "metadata/gateway.json",
            "metadata/registry.json",
            "metadata/accounts.json",
            "forward.pub",
        }
        valid = (
            fixed.issubset(files)
            and all(key in fixed or key.startswith("runtime/") for key in files)
            and "runtime/runtime-manifest.json" in files
        )
    else:
        valid = False
    if not valid or not all(safe_name(key) for key in files):
        raise GateError("package_inventory")


def make_package(role: str, files: dict[str, bytes], pins: dict | None = None) -> tuple[bytes, str]:
    inventory(role, files)
    manifest = {
        "schema": 1,
        "role": role,
        "pins": pins or {},
        "files": [
            {"path": key, "size": len(value), "sha256": sha(value)}
            for key, value in sorted(files.items())
        ],
    }
    contents = {**files, "manifest.json": canonical(manifest)}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, raw in sorted(contents.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(raw), 0o600
            archive.addfile(item, io.BytesIO(raw))
    raw = output.getvalue()
    if len(raw) > MAX_BYTES:
        raise GateError("package_size")
    return raw, sha(raw)


def verify_package(raw: bytes, seal: str, role: str) -> dict[str, bytes]:
    if not HASH.fullmatch(seal) or len(raw) > MAX_BYTES or sha(raw) != seal:
        raise GateError("package_seal")
    contents = {}
    total = 0
    last_end = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for item in archive:
                total += item.size
                if (
                    not safe_name(item.name)
                    or item.name in contents
                    or not item.isfile()
                    or item.uid != 0
                    or item.gid != 0
                    or item.uname
                    or item.gname
                    or item.mode != 0o600
                    or item.pax_headers
                    or not 0 <= item.size <= MAX_BYTES
                    or total > MAX_BYTES
                    or len(contents) > 64
                    or item.name == "manifest.json"
                    and item.size > 65536
                ):
                    raise GateError("package_member")
                stream = archive.extractfile(item)
                if stream is None:
                    raise GateError("package_member")
                contents[item.name] = stream.read(item.size + 1)
                if len(contents[item.name]) != item.size:
                    raise GateError("package_member")
                last_end = item.offset_data + ((item.size + 511) // 512) * 512
        if len(raw) - last_end < 1024 or any(raw[last_end:]):
            raise GateError("package_trailing_data")
        manifest = strict_json(contents.pop("manifest.json"))
        expected = {
            "schema": 1,
            "role": role,
            "pins": manifest["pins"],
            "files": [
                {"path": key, "size": len(value), "sha256": sha(value)}
                for key, value in sorted(contents.items())
            ],
        }
        if manifest != expected or type(manifest["schema"]) is not int:
            raise GateError("package_manifest")
        pins = manifest["pins"]
        if role == "asus":
            if (
                not isinstance(pins, dict)
                or set(pins) != {"source", "runtime", "verifier", "runtime_verifier"}
                or not all(
                    isinstance(value, str) and HASH.fullmatch(value) for value in pins.values()
                )
            ):
                raise GateError("package_pins")
        elif pins != {}:
            raise GateError("package_pins")
        inventory(role, contents)
        return contents
    except (tarfile.TarError, KeyError, TypeError, OverflowError):
        raise GateError("package_invalid") from None


def package_pins(raw: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        stream = archive.extractfile("manifest.json")
        if stream is None:
            raise GateError("package_manifest")
        return strict_json(stream.read(65537))["pins"]


def read_file(path: Path, *, private: bool = False, bound: int = MAX_BYTES) -> bytes:
    path = path.absolute()
    if ".." in path.parts:
        raise GateError("path")
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            owners = {os.geteuid()} if private else {0, os.geteuid()}
            if os.geteuid() == 0 and os.environ.get("SUDO_UID", "").isdigit():
                owners.add(int(os.environ["SUDO_UID"]))
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > bound
                or before.st_uid not in owners
                or (
                    private
                    and (before.st_uid not in owners or stat.S_IMODE(before.st_mode) != 0o600)
                )
            ):
                raise GateError("file_metadata")
            raw = stream.read(bound + 1)
            after = os.fstat(stream.fileno())
            if len(raw) != before.st_size or (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise GateError("file_changed")
            return raw
    finally:
        os.close(parent)


def new_file(path: Path, raw: bytes, mode: int = 0o600, owner: tuple[int, int] = (0, 0)) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        os.fchown(stream.fileno(), *owner)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def replace_file(
    path: Path, raw: bytes, mode: int = 0o600, owner: tuple[int, int] = (0, 0)
) -> None:
    temporary = path.with_name("." + path.name + ".quota-new")
    new_file(temporary, raw, mode, owner)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def new_dir(path: Path, mode: int = 0o700, owner: tuple[int, int] = (0, 0)) -> None:
    path.mkdir(mode=mode)
    path.chmod(mode)
    os.chown(path, *owner)


def trusted_directory(path: Path) -> None:
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GateError("parent_unsafe")


def run(argv: list[str], *, data: bytes | None = None, timeout: int = 30) -> bytes:
    try:
        result = subprocess.run(
            argv,
            input=data,
            stdin=None if data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=ENV,
            umask=0o022,
        )
        if result.returncode or len(result.stdout) > 512 * 1024:
            raise GateError("command_failed")
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise GateError("command_failed") from None


def ask_password() -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            first = getpass.getpass("New independent Quota website password (12..1024 Unicode): ")
            second = getpass.getpass("Repeat Quota website password: ")
        if first != second or not 12 <= len(first) <= 1024 or any(c in first for c in "\r\n\0"):
            raise GateError("dashboard_password_invalid")
        return first.encode("utf-8", "strict")
    except getpass.GetPassWarning:
        raise GateError("secure_tty_required") from None
    except UnicodeError:
        raise GateError("dashboard_password_invalid") from None


def access_document(password: bytes) -> bytes:
    salt = "".join(
        secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        for _ in range(16)
    )
    digest = hashlib.scrypt(
        password, salt=salt.encode(), n=32768, r=8, p=1, dklen=64, maxmem=67108864
    )
    return (
        canonical(
            {
                "schema_version": 1,
                "kind": "scrypt",
                "verifier": "scrypt:32768:8:1$" + salt + "$" + digest.hex(),
                "revision": str(uuid.uuid4()),
            }
        )
        + b"\n"
    )


def password_smoke(native: Path, release: Path, password: bytes) -> None:
    code = (
        "import sys;sys.path.insert(0," + repr(str(release / "src")) + ");"
        "from quota_broker.dashboard_auth import BrowserPasswordFile;"
        "c=BrowserPasswordFile('/etc/api-quota-dashboard/browser-auth.json').read();"
        "p=sys.stdin.buffer.read(4097).decode('utf-8');"
        "assert c.verify(p);print('password-file OK')"
    )
    if (
        run(
            [
                "/usr/sbin/runuser",
                "-u",
                "api-quota-dashboard",
                "--",
                str(native / "bin/python"),
                "-I",
                "-B",
                "-c",
                code,
            ],
            data=password,
            timeout=60,
        ).strip()
        != b"password-file OK"
    ):
        raise GateError("password_file_smoke_failed")


def encrypt(name: str, raw: bytes) -> bytes:
    if name != "forward_key":
        raise GateError("credential_name")
    return run(
        ["/usr/bin/systemd-creds", "encrypt", "--with-key=host", "--name=" + name, "-", "-"],
        data=raw,
    )


def authorized_key(raw: bytes) -> bytes:
    try:
        fields = raw.decode("ascii").strip().split(maxsplit=2)
        if len(fields) < 2 or fields[0] != "ssh-ed25519":
            raise ValueError
        decoded = base64.b64decode(fields[1], validate=True)
        if decoded[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" or len(decoded) != 51:
            raise ValueError
    except (ValueError, UnicodeError):
        raise GateError("public_key_invalid") from None
    return (
        'restrict,port-forwarding,permitopen="127.0.0.1:18586",from="192.168.100.1",command="/usr/bin/false" '
        + " ".join(fields[:2])
        + "\n"
    ).encode("ascii")


def patch_caddy(original: bytes, expected: str) -> bytes:
    if sha(original) != expected:
        raise GateError("caddy_drift")
    site = b"http://:18080 {\n"
    if original.count(site) != 1 or not original.startswith(site) or b"/quota" in original:
        raise GateError("caddy_scope")
    return original.replace(site, site + b"\timport /etc/caddy/quota.caddy\n", 1)


def require_absent(paths: list[Path]) -> None:
    if any(path.exists() or path.is_symlink() for path in paths):
        raise GateError("existing_installation_reconcile")


def require_new_user(name: str) -> None:
    try:
        pwd.getpwnam(name)
    except KeyError:
        pass
    else:
        raise GateError("existing_account_reconcile")
    try:
        grp.getgrnam(name)
    except KeyError:
        return
    raise GateError("existing_group_reconcile")


def port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise GateError("port_in_use") from None


def preflight(role: str) -> None:
    if not sys.stdin.isatty():
        raise GateError("tty_required")
    if os.geteuid() != 0 or socket.gethostname() != HOSTS[role]:
        raise GateError("host_or_uid")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    trusted_directory(Path("/var/lib"))
    trusted_directory(Path("/etc/systemd/system"))
    require_absent([JOURNAL, FORWARD_HOME])
    require_new_user("quota-forward")
    service = "api-quota-dashboard.service" if role == "asus" else "quota-asus-tunnel.service"
    require_absent([Path("/etc/systemd/system") / service])
    if (
        run(["/usr/bin/systemctl", "show", service, "--property=LoadState", "--value"]).strip()
        != b"not-found"
    ):
        raise GateError("existing_unit_reconcile")
    if role == "asus":
        trusted_directory(Path("/opt"))
        trusted_directory(SSH_SCOPE_PATH.parent)
        require_absent([APP, CONFIG, Path("/var/lib/api-quota-dashboard"), SSH_SCOPE_PATH])
        run(["/usr/sbin/sshd", "-t"])
        require_new_user("api-quota-dashboard")
        if str(Path("/opt/api-quota-broker/current").resolve(strict=True)) != BROKER_CURRENT:
            raise GateError("broker_current_drift")
        for unit in ["api-quota-broker.service", "api-quota-broker-client.service"]:
            if run(["/usr/bin/systemctl", "is-active", unit]).strip() != b"active":
                raise GateError("broker_not_active")
        cred = Path("/etc/api-quota-broker/credentials/client_token.cred").lstat()
        if (
            not stat.S_ISREG(cred.st_mode)
            or cred.st_uid != 0
            or stat.S_IMODE(cred.st_mode) != 0o600
        ):
            raise GateError("activity_credential_metadata")
        if (
            run(
                ["/usr/bin/python3.14", "-I", "-B", "-c", "import sys; print(sys.version_info[:3])"]
            ).strip()
            != b"(3, 14, 4)"
        ):
            raise GateError("python_target")
        port_free(18586)
    else:
        trusted_directory(Path("/etc/caddy"))
        require_absent([Path("/etc/quota-forward"), Path("/etc/caddy/quota.caddy")])
        patch_caddy(read_file(Path("/etc/caddy/Caddyfile")), CADDY_SHA)
        for unit in ["caddy.service", "cloudflared.service"]:
            if run(["/usr/bin/systemctl", "is-active", unit]).strip() != b"active":
                raise GateError("ingress_not_active")
        port_free(18095)


def user(name: str, home: str) -> tuple[int, int]:
    run(
        [
            "/usr/sbin/useradd",
            "--system",
            "--user-group",
            "--no-create-home",
            "--home-dir",
            home,
            "--shell",
            "/usr/sbin/nologin",
            name,
        ]
    )
    record = pwd.getpwnam(name)
    return record.pw_uid, record.pw_gid


def unit_install(name: str, raw: bytes) -> None:
    path = Path("/etc/systemd/system") / name
    new_file(path, raw, 0o644)
    run(["/usr/bin/systemd-analyze", "verify", str(path)])
    run(["/usr/bin/systemctl", "daemon-reload"])


def ssh_effective(name: str) -> dict[str, str]:
    if name not in {"quota-forward", "morris"}:
        raise GateError("ssh_user_scope")
    raw = run(
        [
            "/usr/sbin/sshd",
            "-T",
            "-C",
            "user=" + name + ",host=" + HOSTS["asus"] + ",addr=192.168.100.1",
        ]
    )
    result = {}
    for line in raw.decode("ascii").splitlines():
        key, value = line.split(" ", 1)
        result[key] = result[key] + "\n" + value if key in result else value
    return result


def install_ssh_scope() -> None:
    before = ssh_effective("morris")
    new_file(SSH_SCOPE_PATH, SSH_SCOPE, 0o644)
    try:
        run(["/usr/sbin/sshd", "-t"])
        actual = ssh_effective("quota-forward")
        expected = {
            "allowtcpforwarding": "local",
            "permitopen": "127.0.0.1:18586",
            "permitlisten": "none",
            "allowstreamlocalforwarding": "no",
            "allowagentforwarding": "no",
            "x11forwarding": "no",
            "permittty": "no",
            "forcecommand": "/usr/bin/false",
            "passwordauthentication": "no",
            "kbdinteractiveauthentication": "no",
            "authenticationmethods": "publickey",
        }
        if (
            any(actual.get(key) != value for key, value in expected.items())
            or ssh_effective("morris") != before
        ):
            raise GateError("ssh_effective_scope")
        run(["/usr/bin/systemctl", "reload", "ssh.service"])
    except BaseException:
        restore_ssh_scope()
        raise


def restore_ssh_scope() -> None:
    if read_file(SSH_SCOPE_PATH) != SSH_SCOPE:
        raise GateError("ssh_rollback_drift")
    SSH_SCOPE_PATH.unlink()
    run(["/usr/sbin/sshd", "-t"])
    run(["/usr/bin/systemctl", "reload", "ssh.service"])


def stop_new(unit: str) -> None:
    run(["/usr/bin/systemctl", "stop", unit])
    run(["/usr/bin/systemctl", "disable", unit])


def http_status(port: int, path: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "GET", path, headers={"Host": "momonong.me", "X-Forwarded-Proto": "https"}
        )
        response = connection.getresponse()
        response.read(262145)
        return response.status
    finally:
        connection.close()


def ready(port: int) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            if (
                http_status(port, "/quota/") == 200
                and http_status(port, "/quota/api/snapshot") == 401
            ):
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise GateError("anonymous_probe_failed")


def stage_payload(files: dict[str, bytes]) -> Path:
    base = JOURNAL / "payload"
    new_dir(base)
    for name, raw in sorted(files.items()):
        destination = base / name
        for directory in reversed(destination.parent.parents):
            if base in directory.parents and not directory.exists():
                new_dir(directory)
        if destination.parent != base and not destination.parent.exists():
            new_dir(destination.parent)
        new_file(destination, raw)
    return base


def install_asus(files: dict[str, bytes], pins: dict, password: bytes, checkpoint: object) -> None:
    payload = stage_payload(files)
    for name, field in [
        ("build_asus_release.py", "verifier"),
        ("prepare_asus_runtime.py", "runtime_verifier"),
    ]:
        if sha(files["scripts/" + name]) != pins[field]:
            raise GateError("verifier_seal")
    # Runtime verification validates fixed modes; package transport remains private.
    runtime = payload / "runtime"
    manifest = strict_json(files["runtime/runtime-manifest.json"])
    for row in manifest["files"]:
        path = runtime / row["path"]
        if not safe_name(row["path"]) or row["mode"] not in {0o644, 0o755}:
            raise GateError("runtime_manifest")
        path.chmod(row["mode"])
    run(
        [
            "/usr/bin/python3.14",
            "-I",
            "-B",
            str(payload / "scripts/prepare_asus_runtime.py"),
            "verify",
            "--bundle",
            str(runtime),
            "--manifest-sha256",
            pins["runtime"],
            "--source-manifest-sha256",
            pins["source"],
        ],
        timeout=60,
    )
    new_dir(APP, 0o755)
    new_dir(APP / "releases", 0o755)
    release = APP / "releases" / ("quota-" + pins["source"])
    run(
        [
            "/usr/bin/python3.14",
            "-I",
            "-B",
            str(payload / "scripts/build_asus_release.py"),
            "verify",
            str(payload / "source.tar"),
            pins["source"],
            "--extract",
            str(release),
        ],
        timeout=60,
    )
    # Cross-check the verifier bytes against the independently verified source.
    for name in ["build_asus_release.py", "prepare_asus_runtime.py"]:
        if read_file(release / "scripts" / name) != files["scripts/" + name]:
            raise GateError("source_verifier_mismatch")
    release.chmod(0o755)
    for directory in release.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o755)
    uv = runtime / "tools/uv"
    if run([str(uv), "--version"]).split()[:2] != [b"uv", b"0.9.5"]:
        raise GateError("uv_version")
    native = release / "runtime"
    run(
        [
            str(uv),
            "--offline",
            "--no-config",
            "--no-cache",
            "venv",
            "--python",
            "/usr/bin/python3.14",
            "--no-python-downloads",
            "--no-managed-python",
            str(native),
        ],
        timeout=60,
    )
    run(
        [
            str(uv),
            "--offline",
            "--no-config",
            "--no-cache",
            "pip",
            "install",
            "--python",
            str(native / "bin/python"),
            "--no-python-downloads",
            "--no-managed-python",
            "--no-index",
            "--find-links",
            str(runtime / "wheelhouse"),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-deps",
            "-r",
            str(runtime / "requirements.txt"),
        ],
        timeout=180,
    )
    owner = user("api-quota-dashboard", "/var/lib/api-quota-dashboard")
    new_dir(CONFIG, 0o755)
    new_dir(CONFIG / "metadata", owner=owner)
    for name in ["reviewed.json", "gateway.json", "registry.json", "accounts.json"]:
        new_file(CONFIG / "metadata" / name, files["metadata/" + name], owner=owner)
    new_file(CONFIG / "browser-auth.json", access_document(password), 0o640, (0, owner[1]))
    password_smoke(native, release, password)
    # Only this unprivileged process imports application code.
    run(
        [
            "/usr/sbin/runuser",
            "-u",
            "api-quota-dashboard",
            "--",
            str(native / "bin/python"),
            "-I",
            "-B",
            str(release / "scripts/prepare_asus_runtime.py"),
            "--target-smoke",
        ],
        timeout=60,
    )
    user("quota-forward", str(FORWARD_HOME))
    new_dir(FORWARD_HOME, 0o755)
    new_dir(FORWARD_HOME / ".ssh", 0o755)
    checkpoint("ssh_scope")
    install_ssh_scope()
    new_file(FORWARD_HOME / ".ssh/authorized_keys", authorized_key(files["forward.pub"]), 0o644)
    os.symlink(release, APP / "current")
    unit_install("api-quota-dashboard.service", DASHBOARD_UNIT)
    checkpoint("starting")
    run(["/usr/bin/systemctl", "start", "api-quota-dashboard.service"])
    ready(18586)
    run(["/usr/bin/systemctl", "enable", "api-quota-dashboard.service"])


def graph_unchanged(graph: object, main: Path, expected: bytes) -> None:
    if read_file(main) != expected:
        raise GateError("caddy_graph_drift")
    if graph is not None and main in graph.snapshots:
        saved = graph.snapshots[main]
        info = main.lstat()
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
            saved.uid,
            saved.gid,
            saved.mode,
        ):
            raise GateError("caddy_metadata_drift")
    if graph is not None and any(
        not snapshot.unchanged() for path, snapshot in graph.snapshots.items() if path != main
    ):
        raise GateError("caddy_graph_drift")


def trusted_graph() -> object:
    old = Path(HP_HOME_CURRENT)
    trusted_directory(old)
    if Path("/opt/momonong-home/current").resolve(strict=True) != old:
        raise GateError("home_core_current_drift")
    path = old / "scripts/service_passwords_remote.py"
    trusted_directory(path.parent)
    raw = read_file(path)
    info = path.lstat()
    if info.st_uid != 0 or info.st_mode & 0o022 or sha(raw) != HP_HOME_CORE_SHA:
        raise GateError("home_core_pin")
    core = types.ModuleType("quota_trusted_old_core")
    core.__file__ = str(path)
    sys.modules[core.__name__] = core
    exec(compile(raw, str(path), "exec"), core.__dict__)  # noqa: S102 - pinned installed root core
    return core.CaddyGraph("/etc/caddy/Caddyfile", "/etc/caddy/kaching-auth.caddy")


def shared_lock(path: str) -> int:
    target = Path(path)
    trusted_directory(target.parent)
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(fd)
        raise GateError("lock_metadata")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise GateError("shared_operation_busy") from None
    return fd


def include_snapshot(path: Path) -> tuple:
    def identity(info: object) -> tuple:
        return (
            info.st_dev,
            info.st_ino,
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    before = path.lstat()
    raw = read_file(path)
    after = path.lstat()
    if (
        identity(before) != identity(after)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode), after.st_nlink)
        != (0, 0, 0o644, 1)
        or raw != CADDY
    ):
        raise GateError("quota_include_drift")
    return identity(after)


def include_unchanged(path: Path, expected: tuple | None) -> None:
    if expected is None or include_snapshot(path) != expected:
        raise GateError("quota_include_drift")


def restore_hp(
    main: Path,
    include: Path,
    original: bytes,
    candidate_sha: str,
    *,
    started: bool,
    graph: object = None,
    include_state: tuple | None = None,
) -> None:
    if include_state is not None:
        include_unchanged(include, include_state)
    # A backend may still be referenced by the loaded graph until reload succeeds.
    if sha(read_file(main)) != candidate_sha or read_file(include) != CADDY:
        raise GateError("rollback_drift")
    graph_unchanged(graph, main, read_file(main))
    info = main.lstat()
    replace_file(main, original, stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid))
    include.unlink()
    run(["/usr/bin/caddy", "validate", "--config", str(main), "--adapter", "caddyfile"])
    graph_unchanged(graph, main, original)
    run(["/usr/bin/systemctl", "reload", "caddy.service"])
    graph_unchanged(graph, main, original)
    if started:
        stop_new("quota-asus-tunnel.service")


def install_hp(files: dict[str, bytes], checkpoint: object) -> None:
    locks = []
    try:
        for path in ("/run/service-passwords/caddy.lock", "/var/lib/hp-deploy-routes/lock"):
            locks.append(shared_lock(path))
        state = strict_json(read_file(Path("/var/lib/hp-deploy-routes/state.json")))
        if state.get("pending") is not None:
            raise GateError("route_pending")
        install_hp_locked(files, checkpoint)
    finally:
        for fd in reversed(locks):
            os.close(fd)


def install_hp_locked(files: dict[str, bytes], checkpoint: object) -> None:
    graph = trusted_graph()
    main = Path("/etc/caddy/Caddyfile")
    include = Path("/etc/caddy/quota.caddy")
    original = read_file(main)
    main_info = main.lstat()
    if main_info.st_uid != 0 or main_info.st_mode & 0o022:
        raise GateError("caddy_owner_mode")
    candidate = patch_caddy(original, CADDY_SHA)
    new_file(JOURNAL / "Caddyfile.before", original)
    new_file(JOURNAL / "Caddyfile.candidate", candidate)
    encrypted = encrypt("forward_key", files["forward_key"])
    user("quota-forward", str(FORWARD_HOME))
    new_dir(FORWARD_HOME, 0o755)
    config = Path("/etc/quota-forward")
    new_dir(config, 0o755)
    new_file(config / "forward_key.cred", encrypted)
    new_file(config / "known_hosts", ("192.168.100.2 " + HOST_KEY + "\n").encode(), 0o644)
    unit_install("quota-asus-tunnel.service", TUNNEL_UNIT)
    checkpoint("starting")
    run(["/usr/bin/systemctl", "start", "quota-asus-tunnel.service"])
    include_state = None
    try:
        ready(18095)
        new_file(include, CADDY, 0o644)
        include_state = include_snapshot(include)
        run(
            [
                "/usr/bin/caddy",
                "validate",
                "--config",
                str(JOURNAL / "Caddyfile.candidate"),
                "--adapter",
                "caddyfile",
            ]
        )
        graph_unchanged(graph, main, original)
        include_unchanged(include, include_state)
        current_info = main.lstat()
        if (current_info.st_uid, current_info.st_gid, stat.S_IMODE(current_info.st_mode)) != (
            main_info.st_uid,
            main_info.st_gid,
            stat.S_IMODE(main_info.st_mode),
        ):
            raise GateError("caddy_metadata_drift")
        checkpoint("caddy_switch")
        replace_file(
            main, candidate, stat.S_IMODE(main_info.st_mode), (main_info.st_uid, main_info.st_gid)
        )
        graph_unchanged(graph, main, candidate)
        include_unchanged(include, include_state)
        run(["/usr/bin/systemctl", "reload", "caddy.service"])
        graph_unchanged(graph, main, candidate)
        include_unchanged(include, include_state)
        ready(18080)
        run(["/usr/bin/systemctl", "enable", "quota-asus-tunnel.service"])
        graph_unchanged(graph, main, candidate)
        include_unchanged(include, include_state)
    except BaseException:
        # Any drift or reload uncertainty leaves the possibly referenced backend alive.
        if sha(read_file(main)) == sha(candidate):
            restore_hp(
                main,
                include,
                original,
                sha(candidate),
                started=True,
                graph=graph,
                include_state=include_state,
            )
        elif read_file(main) == original:
            graph_unchanged(graph, main, original)
            if include.exists():
                include_unchanged(include, include_state)
                include.unlink()
            stop_new("quota-asus-tunnel.service")
        else:
            raise GateError("rollback_drift") from None
        raise


def apply(role: str, package: Path, seal: str) -> dict:
    preflight(role)  # Non-TTY stop is before all reads, subprocesses, and filesystem mutation.
    raw = read_file(package, private=True)
    return apply_verified_buffer(role, raw, seal)


def apply_buffer(role: str, raw: bytes, seal: str) -> dict:
    preflight(role)
    return apply_verified_buffer(role, raw, seal)


def apply_verified_buffer(role: str, raw: bytes, seal: str) -> dict:
    files = verify_package(raw, seal, role)
    pins = package_pins(raw)
    password = ask_password() if role == "asus" else b""
    new_dir(JOURNAL)
    record = {
        "schema": 1,
        "role": role,
        "operation": seal[:32],
        "package_sha256": seal,
        "status": "running",
        "phase": "preflight",
        "provider_calls": 0,
    }

    def checkpoint(phase: str) -> None:
        record["phase"] = phase
        replace_file(JOURNAL / "receipt.json", canonical(record))

    checkpoint("prepared")
    try:
        if role == "asus":
            install_asus(files, pins, password, checkpoint)
        else:
            install_hp(files, checkpoint)
        record["status"] = "complete"
        checkpoint("complete")
    except BaseException:  # noqa: BLE001 - preserve interrupted privileged outcomes for reconciliation
        record["status"] = "needs_reconcile"
        if role == "asus" and (Path("/etc/systemd/system/api-quota-dashboard.service").exists()):
            try:
                stop_new("api-quota-dashboard.service")
            except GateError:
                record["rollback"] = "stop_unknown"
        if role == "asus" and (FORWARD_HOME / ".ssh/authorized_keys").exists():
            replace_file(FORWARD_HOME / ".ssh/authorized_keys", b"", 0o644)
        if role == "asus" and SSH_SCOPE_PATH.exists():
            try:
                restore_ssh_scope()
            except (GateError, OSError):
                record["rollback"] = "ssh_scope_unknown"
        # HP cleanup belongs exclusively to the full-graph-CAS protected inner gate.
        checkpoint(record["phase"])
        raise GateError("stopped_reconcile_receipt") from None
    return record


def main(role: str) -> int:
    parser = argparse.ArgumentParser(description="One-shot fixed " + role + " /quota gate")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        result = apply(role, args.package, args.sha256)
        print(canonical(result).decode())
        return 0
    except GateError as error:
        print(canonical({"status": "stopped", "reason": str(error)}).decode())
        return 2
    except (KeyboardInterrupt, EOFError):
        print('{"status":"stopped","reason":"operator_aborted"}')
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print('{"status":"stopped","reason":"inspect_fixed_preflight_or_root_receipt"}')
        return 2
