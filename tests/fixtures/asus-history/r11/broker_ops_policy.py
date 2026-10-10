"""Review prototype: fixed Broker operations, fresh Doppler + sudo authentication.

Default prints a proposal only. No account, sudoers, SSH, service or API mutation.
Authentication transport is injected for isolated fixtures; not an installable daemon.
"""

import hashlib
import json
import os
import re
import stat
from pathlib import PurePosixPath

ACCOUNT = "broker-deploy"
CONTROL = "/usr/local/libexec/api-quota-broker-control"
PROJECT, CONFIG = "api-quota-broker-ops", "dev"
SECRET = "ASUS_BROKER_DEPLOY_PASSWORD"
SECRET_PATH = (
    "/v3/configs/config/secret?project=" + PROJECT + "&config=" + CONFIG + "&name=" + SECRET
)
OPERATIONS = {"inspect", "restart"}
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}
READY = b"BROKER_AUTH_READY\n"
SUDOERS = """Defaults:broker-deploy timestamp_timeout=0, passwd_tries=1, !pwfeedback, !use_pty, !rootpw, !targetpw, !noninteractive_auth
broker-deploy ALL=(root:root) PASSWD: /usr/local/libexec/api-quota-broker-control ""
"""


class Denied(ValueError):
    """Fixed codes only; no exception prose from authentication transports."""


def check(condition, code="policy_denied"):
    if not condition:
        raise Denied(code)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, "ambiguous_json")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(Denied("invalid_json")),
    )


def operation_request(raw):
    check(isinstance(raw, bytes) and 0 < len(raw) <= 128, "request_bound")
    try:
        data = strict_json(raw)
        check(type(data) is dict and set(data) == {"operation"}, "request_fields")
        operation = data["operation"]
        check(type(operation) is str and operation in OPERATIONS, "operation_denied")
        return operation
    except (ValueError, TypeError, KeyError, RecursionError):
        raise Denied("operation_denied") from None


def helper_plan(argv, raw):
    check(argv == [], "arguments_denied")
    # EOF from the exact same zero-argument helper is a no-effect probe. No
    # filesystem change or service command occurs until an explicit request.
    if raw == b"":
        return {"auth_gate_open": True, "executed": False, "command": None}
    operation = operation_request(raw)
    command = (
        ("/usr/bin/systemctl", "restart", "api-quota-broker.service")
        if operation == "restart"
        else (
            "/usr/bin/systemctl",
            "show",
            "api-quota-broker.service",
            "--property=ActiveState,SubState,MainPID,NRestarts",
        )
    )
    return {"operation": operation, "executed": False, "command": command}


def read_pinned(anchor, relative, expected_sha, *, owner=0):
    """Descriptor traversal below a pre-verified immutable root-owned anchor.

    Production bootstrap must also verify every ancestor of its fixed anchor.
    No untrusted path supplied by a request is accepted by the root helper.
    """
    parts = PurePosixPath(relative).parts
    check(
        bool(parts)
        and parts == tuple(relative.split("/"))
        and not relative.startswith("/")
        and all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in (".", "..") for p in parts),
        "path_denied",
    )
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            info = os.fstat(fd)
            check(
                stat.S_ISDIR(info.st_mode) and info.st_uid == owner and not info.st_mode & 0o022,
                "directory_untrusted",
            )
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        check(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == owner
            and info.st_nlink == 1
            and not info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
            and info.st_size <= 2097152,
            "file_untrusted",
        )
        raw = bytearray()
        while len(raw) <= 2097152:
            chunk = os.read(fd, min(16384, 2097153 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        check(
            len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == expected_sha,
            "file_not_pinned",
        )
        return bytes(raw)
    finally:
        os.close(fd)


def sudo_command(*, probe=False):
    return ("/usr/bin/sudo", "-k", "-n" if probe else "-S", "-p", "", "--", CONTROL)


def password_from_doppler(token, transport):
    check(type(token) is bytearray and 8 <= len(token) <= 256, "bootstrap_unavailable")
    try:
        # Only the authorization header contains bootstrap credential bytes.
        # Production adapter: fixed api.doppler.com HTTPS, no redirect/proxy,
        # timeout/bounded body, no retry, no CLI/cache/environment injection.
        status, raw = transport(
            SECRET_PATH,
            {"Authorization": "Bearer " + token.decode("ascii"), "Accept": "application/json"},
        )
        check(status == 200 and isinstance(raw, bytes) and len(raw) <= 8192, "doppler_unavailable")
        data = strict_json(raw)
        check(data.get("name") == SECRET, "secret_scope")
        value = data["value"]
        check(
            type(value) is dict
            and type(value.get("raw")) is str
            and value.get("computed") == value["raw"],
            "secret_reference_denied",
        )
        check(bool(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value["raw"])), "password_format")
        return bytearray(value["raw"], "ascii")
    except Exception:  # noqa: BLE001 - never expose HTTP bodies, headers or exception strings.
        raise Denied("doppler_unavailable") from None


def manage(operation, load_token, transport, bridge, memory_guard):
    """One disposable worker; injected interfaces never select executable paths."""
    check(type(operation) is str and operation in OPERATIONS, "operation_denied")
    token, password, session = None, None, None
    try:
        memory_guard()  # swap=0, core=0, non-dumpable BEFORE any secret access.
        probe = bridge.probe(sudo_command(probe=True), dict(SAFE_ENV))
        check(probe == "password_required", "authentication_bypass_or_probe_failure")
        token = load_token()  # only this ops worker's encrypted systemd credential
        password = password_from_doppler(token, transport)
        session = bridge.start(sudo_command(), dict(SAFE_ENV))
        session.password_line(memoryview(password))  # anonymous stdin pipe only
        check(session.ready() == READY, "authentication_failed")
        # A separate handshake prevents PAM read-ahead from eating the request.
        # If NOPASSWD appears after the probe, password stays in helper stdin:
        # its URL-safe form cannot be accepted as operation JSON, so no effects.
        session.commit(json.dumps({"operation": operation}, separators=(",", ":")).encode() + b"\n")
        result = session.result()
        check(
            type(result) is dict
            and result.get("status") == "passed"
            and result.get("operation") == operation
            and result.get("service") == "api-quota-broker.service",
            "operation_unknown",
        )
        receipt = {
            "status": "passed",
            "operation": operation,
            "service": "api-quota-broker.service",
            "secret_saved": False,
        }
    except Exception:  # noqa: BLE001 - every boundary returns fixed safe failure only.
        receipt = {
            "status": "blocked",
            "operation": operation,
            "code": "authentication_or_operation_unverified",
            "automatic_retry": False,
        }
    finally:
        if password is not None:
            password[:] = b"\0" * len(password)
        if type(token) is bytearray:
            token[:] = b"\0" * len(token)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - cleanup must not expose secret-bearing errors.
                receipt = {
                    "status": "blocked",
                    "operation": operation,
                    "code": "worker_cleanup_unverified",
                    "operation_may_have_completed": True,
                    "automatic_retry": False,
                }
        # Bytearray wiping is best effort only. JSON/TLS/PAM copies require the
        # disposable worker/process exit, not a claim of total Python zeroization.
    return receipt


def plan():
    return {
        "mode": "review_only",
        "installable": False,
        "architecture_selected": False,
        "current_pool_recommendation": "existing sealed human TTY path after main review",
        "account": ACCOUNT,
        "account_shell": "/usr/sbin/nologin",
        "account_ssh_keys": False,
        "agent_transport": "existing morris SSH to fixed Unix socket client; SO_PEERCRED UID allowlist",
        "root_helper": CONTROL,
        "root_helper_arguments": [],
        "operations": sorted(OPERATIONS),
        "sudoers": SUDOERS,
        "password_scope": {
            "project": PROJECT,
            "config": CONFIG,
            "secret": SECRET,
            "token_access": "config_read_only",
        },
        "bootstrap": "human creates ops config and expiring read-only token in Doppler UI; hidden TTY entry then encrypted systemd credential",
        "worker": "ASUS broker-deploy UID only; fresh secret GET and sudo -S -k; disposable worker",
        "ops_service_proposed": "api-quota-broker-ops.service",
        "password_crosses_agent_ssh": False,
        "agent_authorization": "fixed operations delegated without interaction; not a second factor",
        "new_release_allowed": False,
        "pool_once_allowed": False,
        "requires_reviewed_artifact_pins": True,
        "host_changes": 0,
        "doppler_calls": 0,
        "provider_calls": 0,
        "limitations": [
            "sudo cannot prove Doppler provenance",
            "Doppler revocation does not revoke acquired passwords",
            "new source needs independent human or trusted-signature approval",
            "native sudo/PAM for nologin account, socket and bootstrap integration still untested",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(plan(), sort_keys=True))
