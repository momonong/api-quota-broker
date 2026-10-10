"""Reviewable complete ops bootstrap. Default never mutates the host.

Human root TTY + sealed private copy + bounded transient unit required for apply.
The original morris password file is never opened, moved, copied or deleted.
"""

import grp
import hashlib
import importlib.util
import json
import os
import pwd
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import termios
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

BOOT_UNIT = "api-quota-broker-ops-bootstrap.service"
BOOT_RUNTIME = 300
ROLLBACK_RESERVE = 60
MUTATION_MIN_REMAINING = 180
ACCOUNT = "broker-deploy"
BASE = Path("/usr/local/lib/api-quota-broker-ops")
CONFIG = Path("/etc/api-quota-broker-ops")
STATE = Path("/var/lib/api-quota-broker-ops")
RUN = Path("/run/api-quota-broker-ops")
SUDOERS = Path("/etc/sudoers.d/api-quota-broker-ops")
LIBEXEC = Path("/usr/local/libexec")
SYSTEM = Path("/etc/systemd/system")
TMPFILES = Path("/etc/tmpfiles.d/api-quota-broker-ops.conf")
CRON_SPOOL = Path("/var/spool/cron/crontabs")
SSH_DENY = Path("/etc/ssh/sshd_config.d/05-api-quota-broker-ops-deny.conf")
SSH_DENY_BYTES = b"# Review only; do not install or reload SSH without main/human authorization.\nDenyUsers broker-deploy\n"
RECOVERY_RECEIPT = Path(
    "/var/backups/api-quota-broker/ops-bootstrap-1e6affdd705a2eeae1bbb5bf1c2c4664/receipt-2e6536c85aca9aa2.json"
)
RECOVERY_MTIME_NS = int("1791260682969549998")
RECOVERY_LEAF_TIME_NS = int("1791260683343542661")
# Canonical bytes expected from the verified r8 writer + human safe projection.
# This is not a claim that the projection exposed a raw receipt digest.
RECOVERY_RECEIPT_SHA = "4a091bae2f003e42457c3e56c4b4032418de84ecf6817844948928dcac90417a"
RECOVERY_CONFIG_PATHS = {
    "/etc/ssh/sshd_config",
    "/etc/ssh/sshd_config.d/50-cloud-init.conf",
    "/etc/default/ssh",
}
RECOVERY_UNIT = "/usr/lib/systemd/system/ssh.service"
RECOVERY_BINARIES = {
    "/usr/sbin/sshd",
    "/usr/lib/openssh/sshd-session",
    "/usr/lib/openssh/sshd-auth",
}
RECOVERY_STEPS = {
    "pin": "recovery_pin_unverified",
    "ssh_source": "recovery_ssh_source_unverified",
    "ssh_effective": "recovery_ssh_effective_unverified",
    "source_fingerprint": "recovery_fingerprint_unverified",
    "ssh_denial": "ssh_deny_not_effective",
    "ssh_syntax": "recovery_syntax_unverified",
    "ssh_reload_interface": "ssh_reload_unverified",
}
NATIVE_TOOL_BITS = {
    "/usr/sbin/sshd": 0,
    "/usr/sbin/visudo": 0,
    "/usr/sbin/useradd": 0,
    "/usr/sbin/usermod": 0,
    "/usr/sbin/chpasswd": 0,
    "/usr/bin/passwd": stat.S_ISUID,
    "/usr/bin/chage": stat.S_ISGID,
    "/usr/sbin/runuser": 0,
    "/usr/bin/sudo": stat.S_ISUID,
    "/usr/bin/systemctl": 0,
    "/usr/bin/systemd-analyze": 0,
    "/usr/bin/systemd-creds": 0,
    "/usr/bin/python3.14": 0,
    "/bin/kill": 0,
}
COMMAND_LABELS = {
    ("/usr/sbin/sshd", "-t"): "ssh_current_syntax",
    ("/usr/sbin/sshd", "-t", "-o", "DenyUsers broker-deploy"): "ssh_candidate_syntax",
    ("/usr/bin/systemctl", "reload", "ssh.service"): "ssh_reload",
    ("/usr/sbin/visudo", "-c"): "sudoers_current_syntax",
    (
        "/usr/bin/systemctl",
        "show",
        BOOT_UNIT,
        "--property=MainPID,ExecMainStartTimestampMonotonic",
    ): "bootstrap_budget_query",
}
for _user in (ACCOUNT, "morris"):
    for _candidate in (False, True):
        _option = ("-o", "DenyUsers broker-deploy") if _candidate else ()
        COMMAND_LABELS[
            (
                "/usr/sbin/sshd",
                "-T",
                *_option,
                "-C",
                "user=" + _user + ",host=localhost,addr=127.0.0.1",
            )
        ] = (
            "ssh_"
            + ("candidate_" if _candidate else "baseline_")
            + ("broker" if _user == ACCOUNT else "morris")
        )
NAMES = (
    "ops_entry.py",
    "broker_ops_policy.py",
    "install_ops.py",
    "api-quota-broker-ops.socket",
    "api-quota-broker-ops@.service",
    "api-quota-broker-control",
    "api-quota-broker-ops-client",
    "broker_ops.sudoers.proposal",
    "api-quota-broker-ops.tmpfiles",
    "broker_ops.ssh-deny.proposal",
)
SAFE_CODES = {
    "host_identity",
    "account_exists",
    "peer_identity",
    "ops_artifact_exists",
    "sudo_version",
    "ops_unit_exists",
    "ssh_authentication_not_isolated",
    "ops_home_exists",
    "ops_job_exists",
    "native_tty_required",
    "human_scope_attestation",
    "expiry_invalid",
    "token_format",
    "tty_multiline",
    "tty_envelope_unverified",
    "tty_eof",
    "tty_bound",
    "tty_noecho_failed",
    "account_identity",
    "password_inactive",
    "host_key_metadata_unverified",
    "effective_sudo_policy_unverified",
    "wrong_password_accepted",
    "unauthorized_or_cached_access",
    "home_created",
    "native_inspect",
    "worker_cleanup_unverified",
    "policy_drift",
    "activation_unverified",
    "services_changed",
    "account_expiry_active",
    "ssh_scope_unverified",
    "credential_tool_unverified",
    "ssh_output_unverified",
    "ssh_deny_not_effective",
    "ssh_deny_list_changed",
    "ssh_other_settings_changed",
    "ssh_source_changed",
    "ssh_reload_unverified",
    "ssh_fragment_exists",
    "ssh_change_authorization_required",
    "native_tool_unverified",
    "proposal_syntax_unverified",
    "chage_interface_unverified",
    "credential_interface_unverified",
    "cron_metadata_unverified",
    "cron_group_unverified",
    "cron_job_path_unverified",
    "cron_schedule_unverified",
    "bootstrap_budget_unverified",
    "bootstrap_budget_insufficient",
    "bootstrap_termination_requested",
    "rollback_budget_exhausted",
    "recovery_pin_unverified",
    "recovery_state_changed",
    "recovery_source_changed",
    "native_diagnostic_blocked",
    "retained_account_unverified",
    "retained_state_unverified",
    "retained_claims_present",
    "retained_policy_unverified",
    "retained_credential_unverified",
    "retained_file_changed",
    "retained_cleanup_unverified",
}
SAFE_CODES |= set(RECOVERY_STEPS.values()) | {"recovery_dependency_unverified"}
SSH_BOOL_FIELDS = (
    "passwordauthentication",
    "kbdinteractiveauthentication",
    "hostbasedauthentication",
    "gssapiauthentication",
)
SSH_FIELDS = (*SSH_BOOL_FIELDS, "authorizedkeyscommand", "trustedusercakeys", "authorizedkeysfile")
SSH_FIELD_CODES = {"ssh_" + name + "_not_isolated" for name in SSH_FIELDS}
SAFE_CODES |= SSH_FIELD_CODES
PREFLIGHT_CHECKS = {
    "identity",
    "artifact_absence",
    "parent_metadata",
    "sudo_version",
    "socket_absence",
    "template_absence",
    "sudoers_syntax",
    "service_baseline",
    "gateway_metadata",
    "broker_pins",
    "host_key",
    "credential_tool",
    "ssh_policy",
    "home_absence",
    "job_absence",
    "receipt_storage",
    "native_tools",
    "proposal_syntax",
    "ssh_candidate",
    "ssh_reload_interface",
    "bootstrap_guard",
    "recovery_pin",
    "retained_state",
}
ENTRY_SAFE_CODES = {
    "native_failed",
    "root_directory_untrusted",
    "directory_mode_untrusted",
    "root_file_untrusted",
    "file_mode_untrusted",
    "root_file_changed",
    "pin_changed",
    "release_changed",
    "release_link_untrusted",
    "release_path_untrusted",
    "runtime_mutable",
    "runtime_link_untrusted",
    "runtime_file_type",
    "unit_dropins_changed",
    "service_unhealthy",
    "core_limit",
    "core_limit_set_failed",
    "core_limit_query_failed",
    "unit_scope",
    "swap_limit",
    "memory_limit",
    "dumpability",
    "sudo_privilege_transition_unavailable",
    "native_exit",
    "native_timeout",
    "native_output_bound",
}
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
CREATE = (
    "/usr/sbin/useradd",
    "--system",
    "--user-group",
    "--no-create-home",
    "--home-dir",
    "/nonexistent",
    "--shell",
    "/usr/sbin/nologin",
    "--expiredate",
    "1",
    "--",
    ACCOUNT,
)
LOCK = ("/usr/sbin/usermod", "--lock", "--expiredate", "1", "--", ACCOUNT)


class Blocked(ValueError):
    """Only static error codes reach receipts."""


class BootstrapStop(BaseException):
    """Fixed interrupt, kept distinct from external/native Exception errors."""


def require(ok, code):
    if not ok:
        raise Blocked(code)


def sealed_sources():
    root = Path(__file__).absolute().parent
    s = root.lstat()
    require(
        root.parent == Path("/var/tmp")
        and re.fullmatch("aqb-ops-bootstrap-[a-f0-9]{32}", root.name)
        and stat.S_ISDIR(s.st_mode)
        and s.st_uid == s.st_gid == 0
        and stat.S_IMODE(s.st_mode) == 0o700,
        "private_bootstrap_required",
    )
    raw = {}
    for name in (*NAMES, "seal.json"):
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            s = os.fstat(f.fileno())
            require(
                stat.S_ISREG(s.st_mode)
                and s.st_uid == s.st_gid == 0
                and s.st_nlink == 1
                and stat.S_IMODE(s.st_mode) == 0o600
                and s.st_size <= 131072,
                "private_source_untrusted",
            )
            raw[name] = f.read(131073)
    seal = json.loads(raw.pop("seal.json"))
    require(
        set(seal) == {"schema", "files"}
        and seal["schema"] == 1
        and set(seal["files"]) == set(NAMES),
        "seal_contract",
    )
    for name in NAMES:
        require(hashlib.sha256(raw[name]).hexdigest() == seal["files"][name], "source_pin_mismatch")
    spec = importlib.util.spec_from_file_location("aqb_bootstrap_entry", root / "ops_entry.py")
    require(spec and spec.loader, "sealed_loader")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return raw, mod


def expiry(value, now):
    d = datetime.fromisoformat(value)
    require(
        d.tzinfo is not None and now + timedelta(minutes=15) < d <= now + timedelta(days=30),
        "expiry_invalid",
    )
    return d.astimezone(UTC).isoformat()


def host_key_metadata(info):
    """Metadata only: systemd's existing host key must never be read or hashed."""
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == info.st_gid == 0
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) in (0o400, 0o600)
        and 0 < info.st_size <= 16384,
        "host_key_metadata_unverified",
    )


def cron_spool_metadata(info, group_gid):
    """Only this fixed Debian/Ubuntu spool permits its protected group write."""
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == 0
        and info.st_gid == group_gid
        and stat.S_IMODE(info.st_mode) == 0o1730
        and info.st_nlink == 2,
        "cron_metadata_unverified",
    )


def path_absent(path):
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return True
    return False


def cron_account_reference(raw):
    """System-crontab direct user column only; never return commands or users."""
    require(
        type(raw) is bytes and len(raw) <= 131072 and b"\0" not in raw, "cron_schedule_unverified"
    )
    macros = {
        b"@reboot",
        b"@yearly",
        b"@annually",
        b"@monthly",
        b"@weekly",
        b"@daily",
        b"@midnight",
        b"@hourly",
    }
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(b"#") or re.match(rb"[A-Za-z_][A-Za-z0-9_]*\s*=", line):
            continue
        fields = line.split(None, 2 if line.startswith(b"@") else 6)
        if line.startswith(b"@"):
            require(len(fields) == 3 and fields[0] in macros, "cron_schedule_unverified")
            owner = fields[1]
        else:
            require(len(fields) == 7, "cron_schedule_unverified")
            owner = fields[5]
        if owner == ACCOUNT.encode("ascii"):
            return True
    return False


def ssh_global_config(main, included):
    """Conservative proof: no Match and only the standard fixed Include.

    Do not extrapolate one loopback -C result across source-dependent Match.
    Unsupported includes or any Match stop before token/account access.
    """
    for index, text in enumerate((main, *included)):
        for line in text.splitlines():
            words = shlex.split(line, comments=True)
            if not words:
                continue
            directive = words[0].lower()
            require(directive != "match", "ssh_scope_unverified")
            if directive == "include":
                require(
                    index == 0 and words[1:] == ["/etc/ssh/sshd_config.d/*.conf"],
                    "ssh_scope_unverified",
                )


def ssh_options_file(text):
    """systemd EnvironmentFile parser subset: only an empty SSHD_OPTS."""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            continue
        require(
            re.fullmatch(r"""\s*SSHD_OPTS\s*=\s*(?:""|''|)\s*""", line) is not None,
            "ssh_scope_unverified",
        )


def ssh_fields(raw):
    """Keep only fixed fields in memory; reject ambiguous native output."""
    found = {}
    for line in raw.decode("utf-8").splitlines():
        name, separator, value = line.partition(" ")
        if separator and name in (*SSH_FIELDS, "denyusers"):
            if name == "denyusers":
                found[name] = (*found.get(name, ()), value.strip())
                continue
            require(name not in found, "ssh_output_unverified")
            found[name] = value.strip()
    return found


def ssh_effective(raw):
    """Full native settings stay in root memory, only equality reaches receipts."""
    found = {}
    for line in raw.decode("utf-8").splitlines():
        name, separator, value = line.partition(" ")
        require(separator, "ssh_output_unverified")
        value = value.strip()
        if name == "denyusers":
            found[name] = (*found.get(name, ()), value)
            continue
        if name in found:
            # sshd -T may repeat HostKey/Subsystem and other list directives.
            # Preserve every value/order; do not drop an earlier setting.
            require(name not in SSH_FIELDS, "ssh_output_unverified")
            previous = found[name]
            found[name] = (*previous, value) if type(previous) is tuple else (previous, value)
        else:
            found[name] = value
    require(all(name in found for name in SSH_FIELDS), "ssh_output_unverified")
    return found


def deny_patterns(fields):
    value = fields.get("denyusers", ())
    if type(value) is str:
        return tuple(value.split())
    require(type(value) is tuple and all(type(v) is str for v in value), "ssh_output_unverified")
    return tuple(pattern for line in value for pattern in line.split())


def exact_ssh_denial(fields):
    return ACCOUNT in deny_patterns(fields)


def ssh_candidate_equal(before, after):
    require(exact_ssh_denial(after), "ssh_deny_not_effective")
    require(
        Counter(deny_patterns(after)) == Counter((*deny_patterns(before), ACCOUNT)),
        "ssh_deny_list_changed",
    )
    require(
        {k: v for k, v in before.items() if k != "denyusers"}
        == {k: v for k, v in after.items() if k != "denyusers"},
        "ssh_other_settings_changed",
    )


def ssh_findings(fields):
    """Only booleans and fixed key-source classes enter diagnostic receipts."""
    findings = []
    for name in SSH_FIELDS:
        value = fields.get(name)
        if name in SSH_BOOL_FIELDS:
            expected = "no"
            actual = (
                value if value in ("yes", "no") else ("missing" if value is None else "unsupported")
            )
            allowed = actual == "no"
        elif name == "authorizedkeysfile":
            expected = "none_or_default_home_relative"
            tokens = value.split() if isinstance(value, str) else []
            if value == "none":
                actual = "none"
            elif tokens and set(tokens) <= {".ssh/authorized_keys", ".ssh/authorized_keys2"}:
                actual = "default_home_relative"
            else:
                actual = "missing" if value is None else "custom_or_external"
            allowed = actual in ("none", "default_home_relative")
        else:
            expected = "none"
            actual = "none" if value == "none" else ("missing" if value is None else "configured")
            allowed = actual == "none"
        if not allowed:
            findings.append({"field": name, "expected": expected, "actual": actual})
    return findings


def tty_line(fd, prompt, *, hidden=False, limit=300):
    require(os.isatty(fd), "native_tty_required")
    settings = termios.tcgetattr(fd)
    raw = bytearray()
    try:
        if hidden:
            changed = settings[:]
            changed[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(fd, termios.TCSAFLUSH, changed)
            require(not termios.tcgetattr(fd)[3] & termios.ECHO, "tty_noecho_failed")
        os.write(fd, prompt.encode("ascii"))
        while len(raw) <= limit:
            b = os.read(fd, 1)
            require(b != b"", "tty_eof")
            if b == b"\n":
                if hidden and select.select([fd], [], [], 0.02)[0]:
                    # Detect an already queued additional canonical paste line.
                    # A CRLF's extra blank LF is harmless; payload is not.
                    extra = bytearray(os.read(fd, limit + 1))
                    try:
                        require(extra in (b"\n", b"\r\n", b"\r"), "tty_multiline")
                    finally:
                        extra[:] = b"\0" * len(extra)
                return raw
            raw.extend(b)
        raise Blocked("tty_bound")
    except BaseException:
        raw[:] = b"\0" * len(raw)
        raise
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, settings)
            if hidden:
                os.write(fd, b"\n")
        except BaseException:
            raw[:] = b"\0" * len(raw)
            raise


def normalize_tty_token(raw):
    """Remove only exact outer transport framing, in mutable memory."""
    require(type(raw) is bytearray and len(raw) <= 300, "tty_envelope_unverified")
    start, end = 0, len(raw)
    while start < end and raw[start] in b" \t\r":
        start += 1
    while end > start and raw[end - 1] in b" \t\r":
        end -= 1
    opening, closing = b"\x1b[200~", b"\x1b[201~"
    has_open = raw.startswith(opening, start, end)
    has_close = raw.endswith(closing, start, end)
    if has_open or has_close:
        require(
            has_open and has_close,
            "tty_envelope_unverified",
        )
        start += 6
        end -= 6
    require(
        not any(raw[i] in (0, 10, 13, 27) for i in range(start, end)), "tty_envelope_unverified"
    )
    raw[:start] = b"\0" * start
    raw[end:] = b"\0" * (len(raw) - end)
    del raw[end:]
    del raw[:start]
    return raw


def initialize(ops):
    """Fixed transaction, effect journal before every fallible mutation."""
    token, password = None, None
    stage = "preflight"
    try:
        ops.preflight()
        stage = "memory_guard"
        if not getattr(ops, "guarded", False):
            ops.guard()
        if getattr(ops, "retained_diagnosis", False):
            return (
                restore_agent_inspect(ops)
                if getattr(ops, "retained_agent", False)
                else diagnose_retained(ops)
            )
        stage = "human_scope_and_token"
        token, expires = ops.input_token()
        if isinstance(ops, NativeBootstrap):
            ops.require_mutation_budget()
        stage = "fresh_doppler"
        password = ops.fetch_password(token)
        if isinstance(ops, NativeBootstrap):
            ops.require_mutation_budget()
        if getattr(ops, "ssh_change_authorized", False):
            stage = "ssh_account_isolation"
            ops.apply_ssh_deny()
        stage = "account_create"
        ops.create_account()
        stage = "account_password"
        ops.set_password(password)
        stage = "sealed_publish"
        ops.publish(token, expires)
        stage = "native_authorization_checks"
        ops.native_checks()
        stage = "native_socket_inspect"
        ops.inspect_once()
        stage = "activation"
        ops.activate()
        stage = "preservation_verify"
        ops.preservation()
        receipt = {
            "status": "passed",
            "mode": "asus_ops_socket_initialized",
            "native_inspect_verified": True,
            "native_restart_executed": False,
            "source_password_file_used": False,
            "source_cleanup_pending": True,
            "scope_verification": "human_dashboard_attestation_only",
            "operations": ["inspect", "restart"],
            "provider_calls": 0,
            "automatic_retry": False,
            "ssh_rule_added": getattr(ops, "ssh_written", False),
            "ssh_preexisting_retained": getattr(ops, "ssh_preexisting", False),
            "ssh_reload_attempted": getattr(ops, "ssh_reload_attempted", False),
            "ssh_reload_verified": getattr(ops, "ssh_reload_verified", False),
            "preflight_checks_passed": getattr(ops, "checks_passed", []),
        }
        ops.receipt(receipt)
        return receipt
    except BaseException as error:  # noqa: BLE001 - rollback/redaction includes interruption.
        failed_command = getattr(ops, "command_label", None)
        recovery_failure = getattr(ops, "recovery_diagnostic", None)
        recovery_step = getattr(ops, "recovery_step", None)
        native_failure = getattr(ops, "native_diagnostic", None)
        native_client_rc = getattr(ops, "native_client_rc", None)
        if getattr(ops, "retained_diagnosis", False) and getattr(ops, "retained_stage", None):
            stage = ops.retained_stage
        try:
            rolled_back = ops.rollback()
        except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
            rolled_back = False
        code = (
            str(error)
            if type(error) is Blocked and str(error) in SAFE_CODES
            else "initialization_unverified"
        )
        if type(error) is BootstrapStop and str(error) in {
            "bootstrap_termination_requested",
            "bootstrap_budget_insufficient",
        }:
            code = str(error)
        entry = getattr(ops, "e", None)
        if (
            entry is not None
            and type(error) is getattr(entry, "Denied", None)
            and str(error) in ENTRY_SAFE_CODES
        ):
            code = str(error)
        if isinstance(error, OSError):
            code = {2: "required_path_missing", 13: "required_path_inaccessible"}.get(
                error.errno, "native_os_failure"
            )
        receipt = {
            "status": "blocked",
            "mode": "asus_ops_socket_initialization",
            "stage": stage,
            "code": code,
            "rollback_verified": rolled_back,
            "manual_recovery_required": not rolled_back,
            "automatic_retry": False,
            "native_restart_executed": False,
            "provider_calls": 0,
        }
        if (
            entry is not None
            and type(error) is getattr(entry, "Denied", None)
            and str(error) in ENTRY_SAFE_CODES
            and type(error.rc) is int
            and -64 <= error.rc <= 255
        ):
            receipt["rc"] = error.rc
        if type(native_client_rc) is int and -64 <= native_client_rc <= 255:
            receipt["native_client_rc"] = native_client_rc
        if recovery_step in RECOVERY_STEPS:
            receipt["recovery_step"] = recovery_step
        if type(native_failure) is dict:
            try:
                receipt["native_diagnostic"] = ops.e.validate_diagnostic(
                    native_failure, allow_unbound=True
                )
            except Exception:  # noqa: BLE001 - never store arbitrary diagnostics.
                receipt["native_diagnostic_unverified"] = True
        if getattr(ops, "retained_diagnosis", False):
            receipt.update(
                retained_contract_restored=rolled_back
                and getattr(ops, "retained_snapshot", None) is not None,
                diagnosis_only=not getattr(ops, "retained_agent", False),
                agent_inspect_ready=False,
                credential_reuse_selected=True,
                credential_consumption_unverified=getattr(ops, "socket_started", False),
                ssh_reload_attempted=False,
                original_live_ssh_state_restored=False,
                public_metadata_restored=False,
                attempt_claim_retained=getattr(ops, "retained_claimed", False),
            )
        if getattr(ops, "continue_pinned_ssh", False):
            receipt.update(
                owned_artifact_rollback_verified=getattr(ops, "owned_rollback_verified", False),
                ssh_preexisting_retained=getattr(ops, "ssh_preexisting", False),
                ssh_reload_attempted=getattr(ops, "ssh_reload_attempted", False),
                ssh_reload_verified=getattr(ops, "ssh_reload_verified", False),
                original_live_ssh_state_restored=False,
            )
        if failed_command in (*COMMAND_LABELS.values(), "sudoers_proposal_syntax"):
            receipt["command"] = failed_command
        if type(recovery_failure) is dict and code == "recovery_source_changed":
            receipt["recovery_source_failure"] = recovery_failure
        checkpoint = getattr(ops, "checkpoint", None)
        if stage == "preflight" and checkpoint in PREFLIGHT_CHECKS:
            receipt["check"] = checkpoint
            if checkpoint in ("socket_absence", "template_absence"):
                receipt["expected"] = "LoadState=not-found"
                receipt["actual"] = (
                    "present_or_unknown" if code == "ops_unit_exists" else "query_failed"
                )
            if checkpoint == "ssh_policy" and code in SSH_FIELD_CODES:
                # Recompute from the internal fixed-field dict, never serialize
                # raw sshd output, custom paths, command argv or exceptions.
                receipt["ssh_mismatches"] = ssh_findings(getattr(ops, "ssh_fields", {}))
            tool = getattr(ops, "native_tool", None)
            if checkpoint == "native_tools" and tool in NATIVE_TOOL_BITS:
                receipt.update(
                    tool=tool,
                    expected="root_protected_executable_known_privilege_bits",
                    actual=code,
                )
            cron_location = getattr(ops, "cron_location", None)
            if checkpoint == "job_absence" and cron_location in (
                "system_crontab",
                "system_directory",
                "user_spool",
            ):
                receipt["cron_location"] = cron_location
        passed = getattr(ops, "checks_passed", [])
        if type(passed) is list:
            receipt["preflight_checks_passed"] = [v for v in passed if v in PREFLIGHT_CHECKS]
        try:
            ops.receipt(receipt)
        except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
            receipt["receipt_persistence_unverified"] = True
        return receipt
    finally:
        for value in (token, password):
            if isinstance(value, bytearray):
                value[:] = b"\0" * len(value)


def diagnose_retained(ops):
    """One inspect attempt; restore retained access state even on success."""
    ops.retained_stage = "retained_recheck"
    ops.require_mutation_budget()
    ops.verify_retained_account()
    require(ops.retained_state() == ops.retained_snapshot, "retained_file_changed")
    require(ops.recovery_sources() == ops.recovery_source_fingerprint, "recovery_source_changed")
    ops.verify_recovery_pin()
    ops.preservation()
    ops.require_mutation_budget()
    ops.retained_stage = "retained_claim"
    ops.claim_retained()
    ops.retained_stage = "retained_publish"
    ops.publish_retained()
    # Existing locked password hash is reused by usermod, never read/reset.
    ops.retained_stage = "retained_account_unlock"
    ops.retained_invariants()
    ops.creation_attempted = True
    ops.run(("/usr/sbin/usermod", "--unlock", "--expiredate", "", "--", ACCOUNT))
    require(
        ops.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2] == [ACCOUNT.encode(), b"P"],
        "password_inactive",
    )
    ops.retained_stage = "native_authorization_checks"
    ops.native_checks()
    ops.retained_stage = "native_socket_inspect"
    ops.retained_invariants()
    ops.inspect_once()
    ops.retained_stage = "retained_cleanup"
    cleaned = ops.rollback()
    require(cleaned, "retained_cleanup_unverified")
    result = {
        "status": "passed",
        "mode": "asus_ops_retained_diagnosis",
        "diagnosis_only": True,
        "native_inspect_verified": True,
        "retained_contract_restored": True,
        "public_bytes_restored": True,
        "public_metadata_restored": False,
        "cipher_metadata_preserved": True,
        "policy_bytes_preserved": True,
        "attempt_claim_retained": True,
        "owned_artifact_rollback_verified": True,
        "credential_reused": True,
        "account_locked_expired": True,
        "ssh_preexisting_retained": True,
        "ssh_reload_attempted": False,
        "original_live_ssh_state_restored": False,
        "native_restart_executed": False,
        "provider_calls": 0,
        "automatic_retry": False,
    }
    ops.receipt(result)
    return result


def restore_agent_inspect(ops):
    """One repair/acceptance transaction, then retain the delegated inspect entry."""
    ops.retained_stage = "retained_recheck"
    ops.require_mutation_budget()
    ops.verify_retained_account()
    require(ops.retained_state() == ops.retained_snapshot, "retained_file_changed")
    require(ops.recovery_sources() == ops.recovery_source_fingerprint, "recovery_source_changed")
    ops.verify_recovery_pin()
    ops.preservation()
    ops.require_mutation_budget()
    ops.retained_stage = "retained_claim"
    ops.claim_retained()
    ops.retained_stage = "retained_publish"
    ops.publish_retained()
    ops.retained_stage = "retained_account_unlock"
    ops.retained_invariants()
    ops.creation_attempted = True
    account_expiry = ops.agent_account_expiry_date()
    ops.run(("/usr/sbin/usermod", "--unlock", "--expiredate", account_expiry, "--", ACCOUNT))
    require(
        ops.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2] == [ACCOUNT.encode(), b"P"],
        "password_inactive",
    )
    ops.retained_stage = "native_authorization_checks"
    ops.native_checks()
    ops.retained_stage = "native_socket_inspect"
    ops.retained_invariants()
    ops.inspect_once()
    ops.retained_stage = "activation"
    ops.enable_agent_inspect()
    ops.preservation()
    result = {
        "status": "passed",
        "mode": "asus_ops_agent_inspect_ready",
        "diagnosis_only": False,
        "agent_inspect_ready": True,
        "operations": ["inspect"],
        "restart_enabled": False,
        "agent_password_prompt": False,
        "credential_reused": True,
        "r13_claim_preserved": True,
        "activation_claim_retained": True,
        "native_inspect_verified": True,
        "native_restart_executed": False,
        "ssh_reload_attempted": False,
        "provider_calls": 0,
        "automatic_retry": False,
        "policy_expiry_not_extended": True,
        "os_account_expiry_bounded": True,
        "account_expiry_date": account_expiry,
    }
    ops.receipt(result)
    return result


class NativeBootstrap:
    def __init__(
        self,
        source,
        entry,
        *,
        ssh_change_authorized=False,
        continue_pinned_ssh=False,
        retained_diagnosis=False,
        retained_agent=False,
    ):
        self.source, self.e = source, entry
        self.created = False
        self.creation_attempted = False
        self.uid = self.gid = None
        self.published = {}
        self.receipt_dir = None
        self.socket_started = False
        self.enabled_link = False
        self.daemon_changed = False
        self.ssh_change_authorized = ssh_change_authorized
        self.ssh_written = False
        self.ssh_write_intent = False
        self.ssh_reload_attempted = False
        self.ssh_reload_verified = False
        self.owned_rollback_verified = False
        self.ssh_needs_change = False
        self.checks_passed = []
        self.checkpoint = None
        self.guarded = False
        self.deadline = None
        self.rolling_back = False
        self.continue_pinned_ssh = continue_pinned_ssh
        self.ssh_preexisting = False
        self.retained_diagnosis = retained_diagnosis or retained_agent
        self.retained_agent = retained_agent
        self.retained_snapshot = None
        self.retained_originals = {}
        self.retained_updated = {}
        self.retained_claimed = False
        self.native_diagnostic = None
        self.native_client_rc = None
        self.recovery_step = None

    def next_check(self, name):
        require(name in PREFLIGHT_CHECKS, "initialization_unverified")
        if self.checkpoint is not None and self.checkpoint not in self.checks_passed:
            self.checks_passed.append(self.checkpoint)
        self.checkpoint = name

    def run(self, argv, *, data=None, limit=16384, timeout=15):
        self.command_label = COMMAND_LABELS.get(tuple(argv))
        if tuple(argv[:3]) == ("/usr/sbin/visudo", "-c", "-f"):
            self.command_label = "sudoers_proposal_syntax"
        if self.deadline is not None:
            stop = self.deadline if self.rolling_back else self.deadline - ROLLBACK_RESERVE
            remaining = stop - time.monotonic()
            require(
                remaining > 1,
                "rollback_budget_exhausted"
                if self.rolling_back
                else "bootstrap_budget_insufficient",
            )
            timeout = min(timeout, remaining - 0.5)
        result = self.e.native(argv, data=data, limit=limit, timeout=timeout)
        self.command_label = None
        return result

    def preflight(self):
        self.next_check("identity")
        require(
            os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server", "host_identity"
        )
        if self.retained_diagnosis:
            require(
                not self.ssh_change_authorized and not self.continue_pinned_ssh,
                "retained_state_unverified",
            )
            require(self.account_identity() == (994, 981), "retained_account_unverified")
            self.uid, self.gid = 994, 981
            self.verify_retained_account()
        else:
            for lookup in (pwd.getpwnam, grp.getgrnam):
                try:
                    lookup(ACCOUNT)
                except KeyError:
                    continue
                raise Blocked("account_exists")
        user = pwd.getpwnam("morris")
        require(user.pw_uid == user.pw_gid == 1000, "peer_identity")
        self.next_check("artifact_absence")
        for p in (
            BASE,
            CONFIG,
            STATE,
            RUN,
            SUDOERS,
            SYSTEM / "api-quota-broker-ops.socket",
            SYSTEM / "api-quota-broker-ops@.service",
            LIBEXEC / "api-quota-broker-control",
            LIBEXEC / "api-quota-broker-ops-client",
            TMPFILES,
            SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket",
        ):
            if not self.retained_diagnosis or p not in (BASE, CONFIG, STATE, RUN):
                require(not p.exists() and not p.is_symlink(), "ops_artifact_exists")
        self.next_check("parent_metadata")
        for p in (
            BASE.parent,
            CONFIG.parent,
            STATE.parent,
            RUN.parent,
            SUDOERS.parent,
            LIBEXEC.parent,
            SYSTEM,
            Path("/etc/tmpfiles.d"),
            Path("/var/backups/api-quota-broker"),
        ):
            self.e.root_dir(p)
        if LIBEXEC.exists():
            self.e.root_dir(LIBEXEC)
        if self.retained_diagnosis:
            self.next_check("recovery_pin")
            self.prepare_retained_ssh()
        elif self.continue_pinned_ssh:
            require(self.ssh_change_authorized, "ssh_change_authorization_required")
            self.next_check("recovery_pin")
            self.verify_recovery_pin()
            self.ssh_preexisting = True
        else:
            # Both ordinary modes retain strict absence; continuation is explicit.
            require(path_absent(SSH_DENY), "ssh_fragment_exists")
        self.next_check("native_tools")
        self.verify_native_tools()
        self.next_check("sudo_version")
        require(
            self.run(("/usr/bin/sudo", "--version")).startswith(b"sudo-rs 0.2.13"), "sudo_version"
        )
        # show requires a valid instantiated unit; @.service is rejected even
        # when its template is absent. The fixed instance resolves a present
        # template too, so not-found still proves the loader found neither.
        for check, unit in (
            ("socket_absence", "api-quota-broker-ops.socket"),
            ("template_absence", "api-quota-broker-ops@preflight.service"),
        ):
            self.next_check(check)
            require(
                self.run(
                    ("/usr/bin/systemctl", "show", unit, "--property=LoadState", "--value")
                ).strip()
                == b"not-found",
                "ops_unit_exists",
            )
        if self.retained_diagnosis:
            require(
                not self.run(
                    (
                        "/usr/bin/systemctl",
                        "list-units",
                        "api-quota-broker-ops@*.service",
                        "--all",
                        "--no-legend",
                        "--no-pager",
                    )
                ).strip(),
                "ops_unit_exists",
            )
            for unit in ("api-quota-broker-ops.socket", "api-quota-broker-ops@preflight.service"):
                require(
                    not self.run(
                        ("/usr/bin/systemctl", "show", unit, "--property=DropInPaths", "--value")
                    ).strip(),
                    "ops_unit_exists",
                )
        self.next_check("sudoers_syntax")
        self.run(("/usr/sbin/visudo", "-c"))
        if self.source:
            self.next_check("proposal_syntax")
            self.run(
                (
                    "/usr/sbin/visudo",
                    "-c",
                    "-f",
                    str(Path(__file__).parent / "broker_ops.sudoers.proposal"),
                )
            )
        self.next_check("service_baseline")
        self.baseline = {
            name: self.e.service_state(name) for name in (self.e.SERVICE, "orderflow.service")
        }
        self.next_check("gateway_metadata")
        self.config_sha = hashlib.sha256(
            self.e.read_root("/etc/api-quota-broker/gateway.json")
        ).hexdigest()
        self.next_check("broker_pins")
        self.e.broker_pins(self.config_sha)
        if self.retained_diagnosis:
            self.next_check("retained_state")
            self.retained_snapshot = self.retained_state()
        self.next_check("host_key")
        self.verify_host_key()
        self.next_check("credential_tool")
        self.verify_credential_tool()
        self.next_check("ssh_policy")
        self.verify_ssh()
        if self.ssh_change_authorized:
            self.next_check("ssh_reload_interface")
            self.verify_ssh_reload()
            self.next_check("ssh_candidate")
            self.prepare_ssh_deny()
        self.next_check("home_absence")
        require(path_absent("/nonexistent"), "ops_home_exists")
        self.next_check("job_absence")
        self.verify_jobs()
        self.next_check("bootstrap_guard")
        self.guard()
        self.guarded = True
        self.next_check("receipt_storage")
        self.receipt_dir = Path("/var/backups/api-quota-broker") / (
            "ops-bootstrap-" + os.urandom(16).hex()
        )
        self.receipt_dir.mkdir(mode=0o700)
        self.receipt(
            {
                "status": "prepared",
                "mode": "asus_ops_socket_bootstrap",
                "provider_calls": 0,
                "preflight_checks_passed": [*self.checks_passed, "receipt_storage"],
                "ssh_change_needed": self.ssh_needs_change,
            }
        )
        self.checks_passed.append("receipt_storage")

    def verify_retained_account(self):
        require(self.account_identity() == (994, 981), "retained_account_unverified")
        require(
            [a.pw_name for a in pwd.getpwall() if a.pw_uid == 994] == [ACCOUNT]
            and [a.pw_name for a in pwd.getpwall() if a.pw_gid == 981] == [ACCOUNT]
            and [g.gr_name for g in grp.getgrall() if g.gr_gid == 981] == [ACCOUNT],
            "retained_account_unverified",
        )
        require(
            self.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2]
            == [ACCOUNT.encode(), b"L"],
            "retained_account_unverified",
        )
        require(
            re.search(
                rb"(?m)^Account expires\s*:\s*1970-01-02\s*$",
                self.run(("/usr/bin/chage", "--list", "--iso8601", ACCOUNT)),
            ),
            "retained_account_unverified",
        )

    def retained_metadata(self, path, *, mode, limit, code="retained_state_unverified"):
        self.e.root_dir(path.parent)
        s = path.lstat()
        require(
            stat.S_ISREG(s.st_mode)
            and s.st_uid == s.st_gid == 0
            and s.st_nlink == 1
            and stat.S_IMODE(s.st_mode) == mode
            and 0 <= s.st_size <= limit,
            code,
        )
        return (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns, s.st_size)

    def retained_state(self):
        """No ciphertext/shadow reads. Unknown claims/history prohibit re-entry."""
        snapshot = {}
        expected = {
            BASE: (0, 0o755, {"ops_entry.py", "broker_ops_policy.py", "manifest.json"}),
            CONFIG: (0, 0o755, {"policy.json", "ops_doppler.cred"}),
            STATE: (0, 0o700, {"operation.lock"} | self.retained_history_names()),
            RUN: (1000, 0o750, set()),
        }
        for path, (gid, mode, names) in expected.items():
            self.e.root_dir(path, mode=mode)
            info = path.lstat()
            require(info.st_gid == gid, "retained_state_unverified")
            # Bound enumeration before collecting potentially attacker-controlled names.
            entries = set()
            for child in path.iterdir():
                require(len(entries) < 16, "retained_claims_present")
                entries.add(child.name)
            require(
                entries == names,
                "retained_claims_present" if path == STATE else "retained_state_unverified",
            )
            snapshot[str(path)] = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
        old_files = {
            "ops_entry.py": "9d3a7ff14f2e7b3ec613bf2f6191a5d8cc97c1a9f7446e7ee30e09ce41953a61",
            "broker_ops_policy.py": "30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522",
        }
        for name, sha in old_files.items():
            self.e.read_root(BASE / name, sha=sha, mode=0o644)
            snapshot[name] = self.retained_metadata(BASE / name, mode=0o644, limit=131072)
        raw = self.e.read_root(BASE / "manifest.json", mode=0o644)
        require(
            raw == json.dumps({"schema": 1, "files": old_files}, sort_keys=True).encode() + b"\n",
            "retained_state_unverified",
        )
        snapshot["manifest"] = hashlib.sha256(raw).hexdigest()
        raw = self.e.read_root(CONFIG / "policy.json", mode=0o644)
        policy = self.e.strict_json(raw)
        expected_policy = {
            "schema": 1,
            "config_sha256": self.config_sha,
            "ops_uid": 994,
            "ops_gid": 981,
            "peer_uid": 1000,
            "scope_verification": "human_dashboard_attestation_only",
            "enabled": False,
            "issued_at": "2026-10-06T09:32:35.390422+00:00",
            "expires_at": "2026-11-05T04:33:31+00:00",
        }
        require(
            raw == json.dumps(expected_policy, sort_keys=True).encode() + b"\n",
            "retained_policy_unverified",
        )
        expiry(policy["expires_at"], datetime.now(UTC))
        snapshot["policy"] = hashlib.sha256(raw).hexdigest()
        snapshot["cipher"] = self.retained_metadata(
            CONFIG / "ops_doppler.cred",
            mode=0o600,
            limit=16384,
            code="retained_credential_unverified",
        )
        require(snapshot["cipher"][-1] > 0, "retained_credential_unverified")
        snapshot["lock"] = self.retained_metadata(STATE / "operation.lock", mode=0o600, limit=0)
        if self.retained_agent:
            snapshot["r13_claim"] = self.verify_r13_claim()
        return snapshot

    def retained_history_names(self):
        return {"retained-inspect.claim.json"} if self.retained_agent else set()

    def retained_current_claim(self):
        return STATE / (
            "agent-inspect-activation.claim.json"
            if self.retained_agent
            else "retained-inspect.claim.json"
        )

    def retained_expected_state_names(self):
        names = {"operation.lock"} | self.retained_history_names()
        if self.retained_claimed:
            names.add(self.retained_current_claim().name)
        return names

    def verify_r13_claim(self):
        # Fixed historical intent only, never replay/delete/modify it.
        raw = (
            json.dumps(
                {
                    "schema": 1,
                    "operation": "inspect",
                    "dispatch_intent": True,
                    "automatic_retry": False,
                    "credential_reuse_selected": True,
                },
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        path = STATE / "retained-inspect.claim.json"
        self.e.read_root(path, sha=hashlib.sha256(raw).hexdigest(), mode=0o600, limit=1024)
        return self.retained_metadata(path, mode=0o600, limit=1024)

    def retained_invariants(self):
        require(self.account_identity() == (994, 981), "retained_account_unverified")
        require(
            self.retained_metadata(CONFIG / "ops_doppler.cred", mode=0o600, limit=16384)
            == self.retained_snapshot["cipher"],
            "retained_file_changed",
        )
        require(
            self.retained_metadata(STATE / "operation.lock", mode=0o600, limit=0)
            == self.retained_snapshot["lock"],
            "retained_file_changed",
        )
        require(
            hashlib.sha256(self.e.read_root(CONFIG / "policy.json", mode=0o644)).hexdigest()
            == self.retained_snapshot["policy"],
            "retained_file_changed",
        )
        require(
            {p.name for p in STATE.iterdir()} == self.retained_expected_state_names(),
            "retained_claims_present",
        )
        require(self.retained_claimed, "retained_state_unverified")
        if self.retained_agent:
            require(
                self.verify_r13_claim() == self.retained_snapshot["r13_claim"],
                "retained_file_changed",
            )
        self.e.read_root(
            self.retained_current_claim(),
            sha=self.retained_claim_sha,
            mode=0o600,
            limit=1024,
        )

    def claim_retained(self):
        # Durable attempt marker blocks all subsequent unattended re-entry.
        # Write intent before any account, public module or permission change.
        claim = {
            "schema": 1,
            "operation": "inspect",
            "dispatch_intent": True,
            "automatic_retry": False,
            "credential_reuse_selected": True,
        }
        if self.retained_agent:
            claim["activation_intent"] = "agent_inspect_only"
        raw = json.dumps(claim, sort_keys=True).encode() + b"\n"
        self.retained_claim_sha = hashlib.sha256(raw).hexdigest()
        self.e.write_exclusive(self.retained_current_claim(), raw)
        self.retained_claimed = True
        self.receipt(
            {
                "status": "prepared",
                "mode": "asus_ops_retained_diagnosis",
                "diagnosis_only": not self.retained_agent,
                "credential_reuse_selected": True,
                "provider_calls": 0,
            }
        )

    def replace_retained(self, path, raw, *, expected_sha):
        # No overwrite/adoption of a drifted file, symlink, or unfinished staging file.
        self.e.read_root(path, sha=expected_sha, mode=0o644)
        temporary = path.with_name(path.name + ".diagnosis-stage")
        require(path_absent(temporary), "retained_file_changed")
        self.e.write_exclusive(temporary, raw, mode=0o644)
        self.e.read_root(path, sha=expected_sha, mode=0o644)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def publish_retained(self):
        require(self.retained_claimed, "retained_state_unverified")
        require(
            hashlib.sha256(self.source["broker_ops_policy.py"]).hexdigest()
            == "30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522",
            "retained_state_unverified",
        )
        manifest = {
            "schema": 1,
            "files": {
                name: hashlib.sha256(self.source[name]).hexdigest()
                for name in ("ops_entry.py", "broker_ops_policy.py")
            },
        }
        replacements = {
            BASE / "ops_entry.py": self.source["ops_entry.py"],
            BASE / "manifest.json": json.dumps(manifest, sort_keys=True).encode() + b"\n",
        }
        # All public originals must be durably preserved before first replacement.
        for path in replacements:
            old = self.e.read_root(path, mode=0o644)
            self.e.write_exclusive(self.receipt_dir / ("retained-original-" + path.name), old)
            self.retained_originals[path] = old
        for path, raw in replacements.items():
            self.retained_updated[path] = hashlib.sha256(raw).hexdigest()
            self.replace_retained(
                path, raw, expected_sha=hashlib.sha256(self.retained_originals[path]).hexdigest()
            )
        for path, name, mode in (
            (
                TMPFILES,
                "api-quota-broker-ops.tmpfiles",
                0o644,
            ),
            (LIBEXEC / "api-quota-broker-control", "api-quota-broker-control", 0o755),
            (LIBEXEC / "api-quota-broker-ops-client", "api-quota-broker-ops-client", 0o755),
            (SYSTEM / "api-quota-broker-ops.socket", "api-quota-broker-ops.socket", 0o644),
            (SYSTEM / "api-quota-broker-ops@.service", "api-quota-broker-ops@.service", 0o644),
            (SUDOERS, "broker_ops.sudoers.proposal", 0o440),
        ):
            self.install(path, self.source[name], mode)
        self.run(("/usr/sbin/visudo", "-c"))
        self.run(
            (
                "/usr/bin/systemd-analyze",
                "verify",
                str(SYSTEM / "api-quota-broker-ops.socket"),
                str(SYSTEM / "api-quota-broker-ops@.service"),
            )
        )
        self.daemon_changed = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))

    def rollback_retained(self):
        # Reuse exact newly-owned access revocation/account lock semantics.
        self.retained_diagnosis = False
        try:
            ok = self.rollback()
        except BaseException:  # noqa: BLE001 - still attempts guarded public restoration.
            ok = False
        finally:
            self.retained_diagnosis = True
        for path, sha in self.retained_updated.items():
            try:
                old = self.retained_originals[path]
                temporary = path.with_name(path.name + ".diagnosis-stage")
                if not path_absent(temporary):
                    self.e.read_root(temporary, sha=sha, mode=0o644)
                    temporary.unlink()
                current = self.e.read_root(path, mode=0o644)
                if hashlib.sha256(current).hexdigest() != hashlib.sha256(old).hexdigest():
                    self.replace_retained(path, old, expected_sha=sha)
            except BaseException:  # noqa: BLE001 - drift is retained, never forced.
                ok = False
        if self.retained_snapshot is not None:
            try:
                for path in (
                    SUDOERS,
                    LIBEXEC / "api-quota-broker-control",
                    LIBEXEC / "api-quota-broker-ops-client",
                    SYSTEM / "api-quota-broker-ops.socket",
                    SYSTEM / "api-quota-broker-ops@.service",
                    TMPFILES,
                    SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket",
                ):
                    require(path_absent(path), "retained_cleanup_unverified")
                # Stopped workers must also be unloaded. Never reset an unknown unit.
                end = time.monotonic() + 8
                for _ in range(40):
                    rows = self.run(
                        (
                            "/usr/bin/systemctl",
                            "list-units",
                            "api-quota-broker-ops@*.service",
                            "--all",
                            "--no-legend",
                            "--no-pager",
                        )
                    )
                    if not rows.strip():
                        break
                    if time.monotonic() >= end:
                        raise Blocked("worker_cleanup_unverified")
                    time.sleep(0.2)
                else:
                    raise Blocked("worker_cleanup_unverified")
                for path, names, mode, gid in (
                    (BASE, {"ops_entry.py", "broker_ops_policy.py", "manifest.json"}, 0o755, 0),
                    (CONFIG, {"policy.json", "ops_doppler.cred"}, 0o755, 0),
                    (RUN, set(), 0o750, 1000),
                ):
                    self.e.root_dir(path, mode=mode)
                    info = path.lstat()
                    require(
                        (info.st_dev, info.st_ino) == self.retained_snapshot[str(path)][:2],
                        "retained_file_changed",
                    )
                    require(
                        path.lstat().st_gid == gid and {p.name for p in path.iterdir()} == names,
                        "retained_state_unverified",
                    )
                self.e.root_dir(STATE, mode=0o700)
                require(STATE.lstat().st_gid == 0, "retained_state_unverified")
                self.e.read_root(
                    BASE / "ops_entry.py",
                    sha="9d3a7ff14f2e7b3ec613bf2f6191a5d8cc97c1a9f7446e7ee30e09ce41953a61",
                    mode=0o644,
                )
                self.e.read_root(
                    BASE / "broker_ops_policy.py",
                    sha="30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522",
                    mode=0o644,
                )
                require(
                    hashlib.sha256(self.e.read_root(BASE / "manifest.json", mode=0o644)).hexdigest()
                    == self.retained_snapshot["manifest"],
                    "retained_file_changed",
                )
                if self.retained_claimed:
                    self.e.read_root(
                        self.retained_current_claim(),
                        sha=self.retained_claim_sha,
                        mode=0o600,
                        limit=1024,
                    )
                self.verify_retained_account()
                require(
                    self.retained_metadata(CONFIG / "ops_doppler.cred", mode=0o600, limit=16384)
                    == self.retained_snapshot["cipher"],
                    "retained_file_changed",
                )
                require(
                    self.retained_metadata(STATE / "operation.lock", mode=0o600, limit=0)
                    == self.retained_snapshot["lock"],
                    "retained_file_changed",
                )
                require(
                    hashlib.sha256(self.e.read_root(CONFIG / "policy.json", mode=0o644)).hexdigest()
                    == self.retained_snapshot["policy"],
                    "retained_file_changed",
                )
                require(
                    {p.name for p in STATE.iterdir()} == self.retained_expected_state_names(),
                    "retained_claims_present",
                )
                if self.retained_agent:
                    require(
                        self.verify_r13_claim() == self.retained_snapshot["r13_claim"],
                        "retained_file_changed",
                    )
            except BaseException:  # noqa: BLE001
                ok = False
        self.owned_rollback_verified = ok
        return ok

    def prepare_retained_ssh(self):
        """Build the effective baseline before its fingerprint consumer."""
        try:
            self.recovery_step = "pin"
            self.verify_recovery_pin()
            self.ssh_preexisting = True
            self.recovery_step = "ssh_source"
            self.ssh_hashes = self.ssh_source()
            require(type(self.ssh_hashes) is dict, "recovery_ssh_source_unverified")
            self.recovery_step = "ssh_effective"
            self.ssh_before = {u: self.effective_ssh(u) for u in (ACCOUNT, "morris")}
            require(
                all(type(v) is dict for v in self.ssh_before.values()),
                "recovery_ssh_effective_unverified",
            )
            self.recovery_step = "source_fingerprint"
            self.recovery_source_fingerprint = self.recovery_sources()
            self.recovery_step = "ssh_denial"
            require(exact_ssh_denial(self.ssh_before[ACCOUNT]), "ssh_deny_not_effective")
            self.recovery_step = "ssh_syntax"
            self.run(("/usr/sbin/sshd", "-t"))
            self.recovery_step = "ssh_reload_interface"
            self.verify_ssh_reload()
        except Exception as error:  # fixed substep, no raw error text.
            if (
                type(error) is Blocked
                and error.args
                and type(error.args[0]) is str
                and error.args[0] in SAFE_CODES
            ):
                raise
            if (
                type(error) is self.e.Denied
                and error.args
                and type(error.args[0]) is str
                and error.args[0] in ENTRY_SAFE_CODES
            ):
                raise
            if isinstance(error, OSError):
                raise
            raise Blocked(RECOVERY_STEPS[self.recovery_step]) from None
        self.recovery_step = None

    def verify_jobs(self):
        # lstat distinguishes absent paths from dangling symlinks. No cron
        # contents or other users' spool entries are read or enumerated.
        for kind, path in (
            ("system_crontab", Path("/etc/crontab")),
            ("system_directory", Path("/etc/cron.d")),
            ("user_spool", CRON_SPOOL),
        ):
            self.cron_location = kind
            self.e.root_dir(path.parent)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if kind == "system_crontab":
                require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == info.st_gid == 0
                    and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) in (0o600, 0o644),
                    "cron_metadata_unverified",
                )
                require(not cron_account_reference(self.e.read_root(path)), "ops_job_exists")
                continue
            if kind == "user_spool":
                try:
                    group = grp.getgrnam("crontab")
                except KeyError:
                    raise Blocked("cron_group_unverified") from None
                require(
                    group.gr_name == "crontab"
                    and 0 < group.gr_gid < 1000
                    and not group.gr_mem
                    and not any(u.pw_gid == group.gr_gid for u in pwd.getpwall()),
                    "cron_group_unverified",
                )
                cron_spool_metadata(info, group.gr_gid)
                try:
                    account = pwd.getpwnam(ACCOUNT)
                except KeyError:
                    account = None
                if account is not None:
                    require(
                        group.gr_gid not in os.getgrouplist(ACCOUNT, account.pw_gid),
                        "cron_group_unverified",
                    )
            else:
                require(
                    stat.S_ISDIR(info.st_mode)
                    and info.st_uid == info.st_gid == 0
                    and not info.st_mode & 0o022,
                    "cron_metadata_unverified",
                )
                self.e.root_dir(path)
                entries = list(path.iterdir())
                require(len(entries) <= 128, "cron_schedule_unverified")
                for item in entries:
                    child = item.lstat()
                    require(
                        stat.S_ISREG(child.st_mode)
                        and child.st_uid == child.st_gid == 0
                        and child.st_nlink == 1
                        and stat.S_IMODE(child.st_mode) in (0o600, 0o644),
                        "cron_metadata_unverified",
                    )
                    require(not cron_account_reference(self.e.read_root(item)), "ops_job_exists")
            try:
                (path / ACCOUNT).lstat()
            except FileNotFoundError:
                continue
            # Any entry, including a dangling symlink/hardlink, is existing work.
            raise Blocked("ops_job_exists")

    def verify_native_tools(self):
        for name, allowed_special in NATIVE_TOOL_BITS.items():
            self.native_tool = name
            path = Path(name)
            # /bin is the root-owned merged-/usr symlink; resolve first.
            self.e.root_dir(path.parent.resolve(strict=True))
            require(path.lstat().st_uid == 0, "native_tool_unverified")
            target = path.resolve(strict=True)
            self.e.root_dir(target.parent)
            info = target.lstat()
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == 0
                and not info.st_mode & 0o022
                and info.st_mode & 0o111
                and info.st_mode & (stat.S_ISUID | stat.S_ISGID) == allowed_special,
                "native_tool_unverified",
            )
        self.native_tool = "/usr/bin/chage"
        help_text = self.run(("/usr/bin/chage", "--help"))
        require(b"--list" in help_text and b"--iso8601" in help_text, "chage_interface_unverified")
        self.native_tool = "/usr/bin/systemd-creds"
        help_text = self.run(("/usr/bin/systemd-creds", "--help"))
        require(
            all(word in help_text for word in (b"encrypt", b"--with-key=", b"--name=")),
            "credential_interface_unverified",
        )

    def ssh_source(self):
        # Prove configuration does not contain source-dependent Match first.
        main_raw = self.e.read_root("/etc/ssh/sshd_config")
        main = main_raw.decode("utf-8")
        hashes = {"/etc/ssh/sshd_config": hashlib.sha256(main_raw).hexdigest()}
        included = []
        for path in sorted(Path("/etc/ssh/sshd_config.d").glob("*.conf")):
            require(len(included) < 128, "ssh_scope_unverified")
            raw = self.e.read_root(path)
            hashes[str(path)] = hashlib.sha256(raw).hexdigest()
            included.append(raw.decode("utf-8"))
        ssh_global_config(main, included)
        settings = self.run(
            (
                "/usr/bin/systemctl",
                "show",
                "ssh.service",
                "--property=ExecStart,Environment,EnvironmentFiles,DropInPaths,FragmentPath",
            )
        ).decode("ascii")
        launch = dict(line.split("=", 1) for line in settings.splitlines())
        require(
            launch.get("DropInPaths") == ""
            and launch.get("Environment") in ("", "SSHD_OPTS=")
            and launch.get("FragmentPath") == "/usr/lib/systemd/system/ssh.service"
            and launch.get("ExecStart", "").count("{ path=") == 1
            and "path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D $SSHD_OPTS ;" in launch["ExecStart"]
            and launch.get("EnvironmentFiles") in ("", "/etc/default/ssh (ignore_errors=yes)"),
            "ssh_scope_unverified",
        )
        if launch["EnvironmentFiles"]:
            # Optional missing file means no override. Existing file must be root protected.
            opts = Path("/etc/default/ssh")
            if opts.exists() or opts.is_symlink():
                raw = self.e.read_root(opts)
                hashes[str(opts)] = hashlib.sha256(raw).hexdigest()
                ssh_options_file(raw.decode("utf-8"))
        return hashes

    def effective_ssh(self, user, *, candidate=False):
        require(user in (ACCOUNT, "morris"), "ssh_scope_unverified")
        option = ("-o", "DenyUsers broker-deploy") if candidate else ()
        return ssh_effective(
            self.run(
                (
                    "/usr/sbin/sshd",
                    "-T",
                    *option,
                    "-C",
                    "user=" + user + ",host=localhost,addr=127.0.0.1",
                ),
                limit=131072,
            )
        )

    def verify_ssh(self):
        self.ssh_source()
        # Repeat after account creation; never broaden SSH/PAM to pass this gate.
        raw = self.run(
            ("/usr/sbin/sshd", "-T", "-C", "user=broker-deploy,host=localhost,addr=127.0.0.1")
        )
        self.ssh_fields = ssh_fields(raw)
        if exact_ssh_denial(self.ssh_fields):
            return
        if self.ssh_change_authorized and not self.ssh_written:
            # Read-only candidate checks follow. Explicit CLI authorization is
            # still required before any SSH file/reload; not a silent fallback.
            self.ssh_needs_change = True
            return
        findings = ssh_findings(self.ssh_fields)
        if findings:
            raise Blocked("ssh_" + findings[0]["field"] + "_not_isolated")

    def verify_ssh_reload(self):
        raw = self.run(
            (
                "/usr/bin/systemctl",
                "show",
                "ssh.service",
                "--property=CanReload,ExecReload,ActiveState,SubState,MainPID",
            )
        )
        lines = [line.partition("=") for line in raw.decode("ascii").splitlines()]
        reloads = [value for name, sep, value in lines if sep and name == "ExecReload"]
        d = {name: value for name, sep, value in lines if sep and name != "ExecReload"}
        require(
            d.get("CanReload") == "yes"
            and d.get("ActiveState") == "active"
            and d.get("SubState") == "running"
            and d.get("MainPID", "").isdigit()
            and int(d["MainPID"]) > 0
            and len(reloads) == 2
            and any("path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ;" in line for line in reloads)
            and any(
                "path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ;" in line for line in reloads
            ),
            "ssh_reload_unverified",
        )
        if not hasattr(self, "ssh_main_pid"):
            self.ssh_main_pid = d["MainPID"]
        require(d["MainPID"] == self.ssh_main_pid, "ssh_reload_unverified")

    def prepare_ssh_deny(self):
        self.ssh_hashes = self.ssh_source()
        self.ssh_before = {user: self.effective_ssh(user) for user in (ACCOUNT, "morris")}
        if self.continue_pinned_ssh:
            require(self.ssh_preexisting and not self.ssh_needs_change, "recovery_pin_unverified")
            require(exact_ssh_denial(self.ssh_before[ACCOUNT]), "ssh_deny_not_effective")
            self.run(("/usr/sbin/sshd", "-t"))
            self.verify_recovery_pin()
            self.recovery_source_fingerprint = self.recovery_sources()
            return
        if not self.ssh_needs_change:
            require(exact_ssh_denial(self.ssh_before[ACCOUNT]), "ssh_deny_not_effective")
            return
        require(
            self.source.get("broker_ops.ssh-deny.proposal") == SSH_DENY_BYTES,
            "proposal_syntax_unverified",
        )
        self.run(("/usr/sbin/sshd", "-t"))
        self.run(("/usr/sbin/sshd", "-t", "-o", "DenyUsers broker-deploy"))
        for user in (ACCOUNT, "morris"):
            ssh_candidate_equal(self.ssh_before[user], self.effective_ssh(user, candidate=True))
        require(self.ssh_source() == self.ssh_hashes, "ssh_source_changed")

    def apply_ssh_deny(self):
        require(self.ssh_change_authorized, "ssh_change_authorization_required")
        if self.continue_pinned_ssh:
            require(
                self.ssh_preexisting and not self.ssh_write_intent and not self.ssh_written,
                "recovery_pin_unverified",
            )
            self.verify_recovery_pin()
            require(
                self.recovery_sources() == self.recovery_source_fingerprint,
                "recovery_source_changed",
            )
            require(self.ssh_source() == self.ssh_hashes, "recovery_state_changed")
            for user in (ACCOUNT, "morris"):
                require(self.effective_ssh(user) == self.ssh_before[user], "recovery_state_changed")
            self.run(("/usr/sbin/sshd", "-t"))
            self.verify_ssh_reload()
            self.audit("ssh_preexisting_reload_intent")
            self.ssh_reload_attempted = True
            # The prepared-only history does not prove live daemon reload.
            # Explicit recovery proposes one reload, never a rewrite/adoption.
            self.run(("/usr/bin/systemctl", "reload", "ssh.service"))
            self.verify_ssh_reload()
            require(self.ssh_source() == self.ssh_hashes, "recovery_state_changed")
            require(
                self.recovery_sources() == self.recovery_source_fingerprint,
                "recovery_source_changed",
            )
            for user in (ACCOUNT, "morris"):
                require(self.effective_ssh(user) == self.ssh_before[user], "recovery_state_changed")
            self.verify_ssh()
            self.preservation()
            self.ssh_reload_verified = True
            self.audit("ssh_preexisting_reload_verified")
            return
        if not self.ssh_needs_change:
            self.verify_ssh()
            return
        require(self.ssh_source() == self.ssh_hashes, "ssh_source_changed")
        self.verify_ssh_reload()
        for user in (ACCOUNT, "morris"):
            require(self.effective_ssh(user) == self.ssh_before[user], "ssh_source_changed")
        self.e.root_dir(SSH_DENY.parent)
        self.audit("ssh_write_intent")
        self.ssh_write_intent = True
        self.e.write_exclusive(SSH_DENY, SSH_DENY_BYTES, mode=0o644)
        self.ssh_written = True
        self.audit("ssh_file_written")
        self.e.read_root(SSH_DENY, sha=hashlib.sha256(SSH_DENY_BYTES).hexdigest(), mode=0o644)
        self.run(("/usr/sbin/sshd", "-t"))
        current_hashes = self.ssh_source()
        require(
            {k: v for k, v in current_hashes.items() if k != str(SSH_DENY)} == self.ssh_hashes,
            "ssh_source_changed",
        )
        for user in (ACCOUNT, "morris"):
            ssh_candidate_equal(self.ssh_before[user], self.effective_ssh(user))
        self.ssh_reload_attempted = True
        self.audit("ssh_reload_intent")
        self.run(("/usr/bin/systemctl", "reload", "ssh.service"))
        self.verify_ssh_reload()
        self.verify_ssh()
        self.preservation()
        self.ssh_reload_verified = True
        self.audit("ssh_reload_verified")

    def verify_host_key(self):
        key = Path("/var/lib/systemd/credential.secret")
        self.e.root_dir(key.parent)
        host_key_metadata(key.lstat())

    def verify_credential_tool(self):
        path = Path("/usr/bin/systemd-creds")
        self.e.root_dir(path.parent)
        info = path.lstat()
        require(info.st_uid == 0, "credential_tool_unverified")
        target = path.resolve(strict=True)
        self.e.root_dir(target.parent)
        info = target.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
            and info.st_mode & 0o111,
            "credential_tool_unverified",
        )
        require(
            self.run(("/usr/bin/systemd-creds", "--version")).startswith(b"systemd 259"),
            "credential_tool_unverified",
        )

    def guard(self):
        self.e.memory_guard(bootstrap=True)
        require(os.isatty(0) and os.isatty(1) and os.isatty(2), "native_tty_required")
        raw = self.run(
            (
                "/usr/bin/systemctl",
                "show",
                BOOT_UNIT,
                "--property=MainPID,ExecMainStartTimestampMonotonic",
            )
        )
        fields = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())
        require(
            set(fields) == {"MainPID", "ExecMainStartTimestampMonotonic"}
            and fields["MainPID"] == str(os.getpid())
            and fields["ExecMainStartTimestampMonotonic"].isdigit(),
            "bootstrap_budget_unverified",
        )
        started = int(fields["ExecMainStartTimestampMonotonic"]) / 1_000_000
        require(
            0 < started <= time.monotonic() < started + BOOT_RUNTIME, "bootstrap_budget_unverified"
        )
        self.deadline = started + BOOT_RUNTIME
        self.arm_budget_timer()

    def arm_budget_timer(self):
        if self.deadline is not None:
            stop = self.deadline if self.rolling_back else self.deadline - ROLLBACK_RESERVE
            remaining = stop - time.monotonic()
            require(
                remaining > 0,
                "rollback_budget_exhausted"
                if self.rolling_back
                else "bootstrap_budget_insufficient",
            )
            signal.setitimer(signal.ITIMER_REAL, remaining)

    def require_mutation_budget(self):
        require(
            self.deadline is not None
            and self.deadline - time.monotonic() >= MUTATION_MIN_REMAINING,
            "bootstrap_budget_insufficient",
        )

    def audit(self, point):
        require(
            point
            in {
                "ssh_write_intent",
                "ssh_file_written",
                "ssh_reload_intent",
                "ssh_reload_verified",
                "ssh_preexisting_reload_intent",
                "ssh_preexisting_reload_verified",
            },
            "initialization_unverified",
        )
        self.receipt(
            {
                "status": "prepared",
                "mode": "asus_ops_socket_bootstrap_progress",
                "checkpoint": point,
                "ssh_preexisting": self.ssh_preexisting,
                "provider_calls": 0,
            }
        )

    def verify_recovery_pin(self):
        try:
            self.e.root_dir(RECOVERY_RECEIPT.parent, mode=0o700)
            info = RECOVERY_RECEIPT.lstat()
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == info.st_gid == 0
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_mtime_ns == RECOVERY_MTIME_NS,
                "recovery_pin_unverified",
            )
            # No unknown original-directory entries or later final/progress.
            require(
                {p.name for p in RECOVERY_RECEIPT.parent.iterdir()} == {RECOVERY_RECEIPT.name},
                "recovery_pin_unverified",
            )
            self.e.read_root(RECOVERY_RECEIPT, sha=RECOVERY_RECEIPT_SHA, mode=0o600, limit=16384)
            self.e.root_dir(SSH_DENY.parent)
            info = SSH_DENY.lstat()
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == info.st_gid == 0
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o644,
                "recovery_pin_unverified",
            )
            require(
                info.st_mtime_ns == info.st_ctime_ns == RECOVERY_LEAF_TIME_NS,
                "recovery_pin_unverified",
            )
            self.e.read_root(SSH_DENY, sha=hashlib.sha256(SSH_DENY_BYTES).hexdigest(), mode=0o644)
        except Exception:  # noqa: BLE001 - pin failure never exposes private receipt contents.
            raise Blocked("recovery_pin_unverified") from None

    def recovery_sources(self):
        # Exact known source set from this ASUS incident. No later config is
        # silently activated by the proposed shared-daemon reload.
        self.recovery_diagnostic = None
        require(
            type(getattr(self, "ssh_before", None)) is dict
            and set(self.ssh_before) == {ACCOUNT, "morris"}
            and all(type(v) is dict for v in self.ssh_before.values()),
            "recovery_dependency_unverified",
        )
        hashes = self.ssh_source()
        self.recovery_require(set(hashes) == RECOVERY_CONFIG_PATHS | {str(SSH_DENY)}, "source_set")
        fingerprint = {}
        for name in sorted(RECOVERY_CONFIG_PATHS | {RECOVERY_UNIT} | RECOVERY_BINARIES):
            path = Path(name)
            raw = self.e.read_root(
                path, limit=16 * 1024 * 1024 if name in RECOVERY_BINARIES else 131072
            )
            info = path.lstat()
            self.recovery_require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == info.st_gid == 0
                and info.st_nlink == 1,
                "file_metadata",
                path=name,
            )
            self.recovery_require(info.st_mtime_ns <= RECOVERY_MTIME_NS, "mtime_fence", path=name)
            self.recovery_require(info.st_ctime_ns <= RECOVERY_MTIME_NS, "ctime_fence", path=name)
            fingerprint[name] = (
                info.st_dev,
                info.st_ino,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_size,
                hashlib.sha256(raw).hexdigest(),
            )
        # Detect subsequent additions/removals in the include directory; its
        # original update for this exact leaf may follow the prepared receipt.
        parent = SSH_DENY.parent.lstat()
        leaf = SSH_DENY.lstat()
        self.recovery_require(
            parent.st_mtime_ns <= leaf.st_mtime_ns and parent.st_ctime_ns <= leaf.st_ctime_ns,
            "include_directory_time",
            path=str(SSH_DENY.parent),
        )
        for user, values in self.ssh_before.items():
            for field, expected in (
                ("sshdsessionpath", "/usr/lib/openssh/sshd-session"),
                ("sshdauthpath", "/usr/lib/openssh/sshd-auth"),
            ):
                self.recovery_require(
                    values.get(field) == expected,
                    "helper_effective_path",
                    user=user,
                    field=field,
                    actual="missing" if field not in values else "different",
                )
        return fingerprint

    def recovery_require(self, ok, predicate, *, path=None, user=None, field=None, actual=None):
        if ok:
            return
        # Only fixed predicates/paths/users/field keys enter receipts. Never
        # serialize custom sshd argv, arbitrary paths, raw settings or secrets.
        require(
            predicate
            in {
                "source_set",
                "file_metadata",
                "mtime_fence",
                "ctime_fence",
                "include_directory_time",
                "helper_effective_path",
            },
            "recovery_source_changed",
        )
        diagnostic = {"predicate": predicate}
        if (
            path
            in RECOVERY_CONFIG_PATHS | {RECOVERY_UNIT, str(SSH_DENY.parent)} | RECOVERY_BINARIES
        ):
            diagnostic["path"] = path
        if user in (ACCOUNT, "morris"):
            diagnostic["user"] = user
        if field in ("sshdsessionpath", "sshdauthpath"):
            diagnostic["field"] = field
        if actual in ("missing", "different"):
            diagnostic["actual"] = actual
        self.recovery_diagnostic = diagnostic
        raise Blocked("recovery_source_changed")

    def input_token(self):
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NOFOLLOW)
        token = None
        try:
            raw = tty_line(
                fd,
                "Prepared token: api-quota-broker-ops/dev, READ ONLY, remaining <=30 days. Type READONLY30: ",
                limit=16,
            )
            require(raw == b"READONLY30", "human_scope_attestation")
            exp = tty_line(fd, "Actual Dashboard expiry UTC (YYYY-MM-DDTHH:MM:SSZ): ", limit=40)
            expires = expiry(exp.decode("ascii"), datetime.now(UTC))
            token = tty_line(fd, "Ops Service Token (hidden, one input): ", hidden=True)
            normalize_tty_token(token)
            require(self.e.valid_service_token(token), "token_format")
            return token, expires
        except BaseException:
            self.e.wipe(token)
            raise
        finally:
            os.close(fd)

    def fetch_password(self, token):
        root = Path(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "aqb_bootstrap_policy", root / "broker_ops_policy.py"
        )
        require(spec and spec.loader, "policy_loader")
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        return policy.password_from_doppler(token, self.e.doppler_transport)

    def account_identity(self):
        a, g = pwd.getpwnam(ACCOUNT), grp.getgrnam(ACCOUNT)
        runtime = pwd.getpwnam("api-quota-broker")
        require(
            0 < a.pw_uid < 1000
            and a.pw_uid != runtime.pw_uid
            and 0 < g.gr_gid < 1000
            and g.gr_gid != runtime.pw_gid
            and a.pw_gid == g.gr_gid
            and not g.gr_mem
            and set(os.getgrouplist(ACCOUNT, g.gr_gid)) == {g.gr_gid}
            and a.pw_dir == "/nonexistent"
            and a.pw_shell == "/usr/sbin/nologin",
            "account_identity",
        )
        return a.pw_uid, a.pw_gid

    def create_account(self):
        self.creation_attempted = True
        self.run(CREATE)
        self.created = True
        self.uid, self.gid = self.account_identity()

    def set_password(self, password):
        payload = bytearray(ACCOUNT.encode() + b":")
        payload.extend(password)
        payload.extend(b"\n")
        try:
            self.run(("/usr/sbin/chpasswd",), data=memoryview(payload))
            # PAM authentication must be active during native validation.
            self.run(("/usr/sbin/usermod", "--expiredate", "", "--", ACCOUNT))
            require(
                self.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2]
                == [ACCOUNT.encode(), b"P"],
                "password_inactive",
            )
        finally:
            self.e.wipe(payload)

    def install(self, path, raw, mode=0o644):
        # Track intent first: partial writes still need rollback ownership checks.
        self.published[path] = hashlib.sha256(raw).hexdigest()
        self.e.write_exclusive(path, raw, mode=mode)

    def policy_bytes(self, *, enabled):
        return (
            json.dumps(
                {
                    "schema": 1,
                    "expires_at": self.expires,
                    "issued_at": self.issued,
                    "config_sha256": self.config_sha,
                    "ops_uid": self.uid,
                    "ops_gid": self.gid,
                    "peer_uid": 1000,
                    "scope_verification": "human_dashboard_attestation_only",
                    "enabled": enabled,
                },
                sort_keys=True,
            ).encode()
            + b"\n"
        )

    def publish(self, token, expires):
        self.expires = expires
        self.issued = datetime.now(UTC).isoformat()
        for p in (BASE, CONFIG, STATE):
            mode = 0o700 if p == STATE else 0o755
            p.mkdir(mode=mode)
            p.chmod(mode)
        RUN.mkdir(mode=0o750)
        RUN.chmod(0o750)
        os.chown(RUN, 0, 1000)
        if not LIBEXEC.exists():
            LIBEXEC.mkdir(mode=0o755)
            LIBEXEC.chmod(0o755)
        self.install(
            TMPFILES,
            self.source["api-quota-broker-ops.tmpfiles"],
        )
        for name in ("ops_entry.py", "broker_ops_policy.py"):
            self.install(BASE / name, self.source[name])
        self.install(
            BASE / "manifest.json",
            json.dumps(
                {
                    "schema": 1,
                    "files": {
                        n: hashlib.sha256(self.source[n]).hexdigest()
                        for n in ("ops_entry.py", "broker_ops_policy.py")
                    },
                },
                sort_keys=True,
            ).encode()
            + b"\n",
        )
        self.install(CONFIG / "policy.json", self.policy_bytes(enabled=False))
        self.install(STATE / "operation.lock", b"", 0o600)
        # Host key already exists from Broker deployment. Never create/replace it.
        self.verify_host_key()
        ciphertext = self.run(
            (
                "/usr/bin/systemd-creds",
                "encrypt",
                "--with-key=host",
                "--name=ops_doppler",
                "-",
                "-",
            ),
            data=memoryview(token),
            limit=16384,
        )
        self.install(CONFIG / "ops_doppler.cred", ciphertext, 0o600)
        for name in ("api-quota-broker-control", "api-quota-broker-ops-client"):
            self.install(LIBEXEC / name, self.source[name], 0o755)
        for name in ("api-quota-broker-ops.socket", "api-quota-broker-ops@.service"):
            self.install(SYSTEM / name, self.source[name])
        self.install(SUDOERS, self.source["broker_ops.sudoers.proposal"], 0o440)
        self.run(("/usr/sbin/visudo", "-c"))
        self.run(
            (
                "/usr/bin/systemd-analyze",
                "verify",
                str(SYSTEM / "api-quota-broker-ops.socket"),
                str(SYSTEM / "api-quota-broker-ops@.service"),
            )
        )
        self.daemon_changed = True
        self.run(("/usr/bin/systemctl", "daemon-reload"))

    def as_ops(self, argv, *, data=None, timeout=20):
        return self.run(
            ("/usr/sbin/runuser", "-u", ACCOUNT, "--", *argv), data=data, timeout=timeout
        )

    def native_checks(self):
        self.verify_ssh()
        self.verify_jobs()
        self.verify_active_account_expiry()
        # All policy listing is non-executing. No arbitrary privileged program.
        raw = self.run(("/usr/bin/sudo", "-l", "-U", ACCOUNT), limit=16384).decode("ascii")
        lines = [l.strip() for l in raw.splitlines() if l.lstrip().startswith("(")]
        require(
            len(lines) == 1
            and re.fullmatch(
                r'\(root\s*:\s*root\)\s+(?:PASSWD:\s*)?/usr/local/libexec/api-quota-broker-control(?:\s+""|)',
                lines[0],
            ),
            "effective_sudo_policy_unverified",
        )
        # Wrong password is public synthetic input; never read the real password
        # into a diagnostic child or execute a privileged operation on failure.
        p = subprocess.run(
            ("/usr/sbin/runuser", "-u", ACCOUNT, "--", *self.e.sudo_argv()),
            input=b"INVALID_PUBLIC_OPS_FIXTURE\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(ENV),
            timeout=15,
            check=False,
        )
        require(p.returncode != 0 and self.e.READY not in p.stdout, "wrong_password_accepted")
        for argv in (
            self.e.sudo_argv(True),
            ("/usr/bin/sudo", "-k", "-n", "-p", "", "--", self.e.CONTROL, "inspect"),
            ("/usr/bin/sudo", "-k", "-n", "-p", "", "--", "/usr/bin/true"),
        ):
            p = subprocess.run(
                ("/usr/sbin/runuser", "-u", ACCOUNT, "--", *argv),
                input=b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(ENV),
                timeout=10,
                check=False,
            )
            require(
                p.returncode != 0 and self.e.READY not in p.stdout, "unauthorized_or_cached_access"
            )
        for command in ((self.e.CONTROL, "inspect"), ("/usr/bin/true",)):
            p = subprocess.run(
                ("/usr/bin/sudo", "-l", "-U", ACCOUNT, "--", *command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(ENV),
                timeout=10,
                check=False,
            )
            require(p.returncode != 0, "unauthorized_or_cached_access")
        # Absence of authorized keys plus effective SSH auth restrictions gates
        # password/SFTP/forwarding admission; no sshd or PAM modifications.
        require(path_absent("/nonexistent"), "home_created")
        self.run(("/usr/bin/passwd", "--status", ACCOUNT))

    def inspect_once(self):
        req = None
        stage = "native_socket_start"
        try:
            self.socket_started = True
            self.run(("/usr/bin/systemctl", "start", "api-quota-broker-ops.socket"))
            stage = "native_client"
            timeout = 45
            if self.deadline is not None:
                remaining = self.deadline - ROLLBACK_RESERVE - time.monotonic()
                require(remaining > 1, "bootstrap_budget_insufficient")
                timeout = min(timeout, remaining - 0.5)
            rc, raw = self.e.native_result(
                (
                    "/usr/sbin/runuser",
                    "-u",
                    "morris",
                    "--",
                    str(LIBEXEC / "api-quota-broker-ops-client"),
                    "inspect",
                ),
                timeout=timeout,
                limit=4096,
            )
            self.native_client_rc = rc
            result = self.e.strict_json(raw)
            if type(result) is dict and result.get("status") == "blocked":
                value = self.e.validate_diagnostic(result, allow_unbound=True)
                require(rc == 1 and value["operation"] in (None, "inspect"), "native_inspect")
                self.native_diagnostic = value
                self.receipt(
                    {
                        "status": "blocked",
                        "mode": "asus_ops_native_diagnostic",
                        "native_diagnostic": value,
                        "native_client_rc": rc,
                        "provider_calls": 0,
                    }
                )
                raise Blocked("native_diagnostic_blocked")
            require(
                type(result) is dict
                and set(result)
                == {"status", "operation", "request_id", "service", "state", "automatic_retry"}
                and rc == 0
                and result["status"] == "passed"
                and result["operation"] == "inspect"
                and result["service"] == self.e.SERVICE
                and result["automatic_retry"] is False
                and type(result["request_id"]) is str
                and re.fullmatch("[a-f0-9]{32}", result["request_id"]),
                "native_inspect",
            )
            require(result["state"] == self.e.service_state(), "native_inspect")
            req = {"operation": "inspect", "request_id": result["request_id"]}
        except BaseException as error:  # fixed projection before rollback.
            if self.native_diagnostic is None:
                self.native_diagnostic = self.e.diagnostic(
                    stage, error, req=req, rc=self.native_client_rc
                )
                self.receipt(
                    {
                        "status": "blocked",
                        "mode": "asus_ops_native_diagnostic",
                        "native_diagnostic": self.native_diagnostic,
                        "native_client_rc": self.native_client_rc,
                        "provider_calls": 0,
                    }
                )
            raise
        # Socket activation jobs must fully exit before considering readiness.
        end = time.monotonic() + 8
        while time.monotonic() < end:
            active = self.run(
                (
                    "/usr/bin/systemctl",
                    "list-units",
                    "api-quota-broker-ops@*.service",
                    "--state=active,activating,deactivating",
                    "--no-legend",
                    "--no-pager",
                )
            )
            if not active.strip():
                break
            time.sleep(0.2)
        else:
            self.native_diagnostic = self.e.diagnostic(
                "native_worker_cleanup", self.e.Denied("worker_cleanup_unverified"), req=req
            )
            raise Blocked("worker_cleanup_unverified")
        self.preservation()

    def agent_account_expiry_date(self):
        raw = self.e.read_root(CONFIG / "policy.json", mode=0o644)
        require(hashlib.sha256(raw).hexdigest() == self.retained_snapshot["policy"], "policy_drift")
        policy = self.e.strict_json(raw)
        end = datetime.fromisoformat(policy["expires_at"])
        require(end.tzinfo is not None, "expiry_invalid")
        # Shadow expiry is a date. Use the UTC date, possibly earlier than the token's time.
        return end.astimezone(UTC).date().isoformat()

    def verify_active_account_expiry(self):
        expected = self.agent_account_expiry_date() if self.retained_agent else "never"
        require(
            re.search(
                rb"(?m)^Account expires\s*:\s*" + re.escape(expected.encode("ascii")) + rb"\s*$",
                self.run(("/usr/bin/chage", "--list", "--iso8601", ACCOUNT)),
            ),
            "account_expiry_active",
        )

    def enable_agent_inspect(self):
        # Policy stays disabled for restart. Only the socket/inspect path is kept.
        self.retained_invariants()
        require(self.e.runtime_policy()["enabled"] is False, "policy_drift")
        self.verify_agent_public_entry()
        self.enabled_link = True
        self.run(("/usr/bin/systemctl", "enable", "api-quota-broker-ops.socket"))
        link = SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket"
        info = link.lstat()
        require(
            stat.S_ISLNK(info.st_mode)
            and info.st_uid == info.st_gid == 0
            and link.resolve(strict=True) == SYSTEM / "api-quota-broker-ops.socket",
            "activation_unverified",
        )
        require(
            self.run(("/usr/bin/systemctl", "is-active", "api-quota-broker-ops.socket")).strip()
            == b"active",
            "activation_unverified",
        )
        require(
            self.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2]
            == [ACCOUNT.encode(), b"P"],
            "password_inactive",
        )
        self.verify_active_account_expiry()
        self.verify_ssh()
        self.verify_jobs()
        self.retained_invariants()
        self.verify_agent_public_entry()

    def verify_agent_public_entry(self):
        for path, sha in self.published.items():
            mode = 0o440 if path == SUDOERS else 0o755 if path.parent == LIBEXEC else 0o644
            self.e.read_root(path, sha=sha, mode=mode)
        for path, sha in self.retained_updated.items():
            self.e.read_root(path, sha=sha, mode=0o644)
        self.e.read_root(
            BASE / "broker_ops_policy.py",
            sha="30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522",
            mode=0o644,
        )

    def activate(self):
        old = self.e.read_root(CONFIG / "policy.json", mode=0o644)
        require(
            hashlib.sha256(old).hexdigest() == self.published[CONFIG / "policy.json"],
            "policy_drift",
        )
        new = self.policy_bytes(enabled=True)
        stage = CONFIG / "policy.ready.json"
        self.e.write_exclusive(stage, new, mode=0o644)
        os.replace(stage, CONFIG / "policy.json")
        fd = os.open(CONFIG, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.published[CONFIG / "policy.json"] = hashlib.sha256(new).hexdigest()
        self.enabled_link = True
        self.run(("/usr/bin/systemctl", "enable", "api-quota-broker-ops.socket"))
        require(
            self.run(("/usr/bin/systemctl", "is-active", "api-quota-broker-ops.socket")).strip()
            == b"active",
            "activation_unverified",
        )

    def preservation(self):
        self.e.broker_pins(self.config_sha)
        require(
            {n: self.e.service_state(n) for n in self.baseline} == self.baseline, "services_changed"
        )

    def rollback(self):
        if self.retained_diagnosis:
            return self.rollback_retained()
        self.rolling_back = True
        if self.deadline is not None:
            try:
                self.arm_budget_timer()
            except Blocked:
                return False
        ok = True
        if self.socket_started or self.enabled_link:
            try:
                link = SYSTEM / "sockets.target.wants/api-quota-broker-ops.socket"
                if not path_absent(link):
                    info = link.lstat()
                    require(
                        self.enabled_link
                        and stat.S_ISLNK(info.st_mode)
                        and info.st_uid == info.st_gid == 0
                        and link.resolve(strict=True) == SYSTEM / "api-quota-broker-ops.socket",
                        "rollback_artifact_drift",
                    )
                self.run(("/usr/bin/systemctl", "disable", "--now", "api-quota-broker-ops.socket"))
                self.run(("/usr/bin/systemctl", "stop", "api-quota-broker-ops@*.service"))
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        if self.created or self.creation_attempted:
            try:
                actual = self.account_identity()
                require(self.uid is None or actual == (self.uid, self.gid), "rollback_uid_drift")
                self.uid, self.gid = actual
                self.run(LOCK)
                require(
                    self.run(("/usr/bin/passwd", "--status", ACCOUNT)).split()[:2]
                    == [ACCOUNT.encode(), b"L"],
                    "rollback_lock",
                )
                require(
                    re.search(
                        rb"(?m)^Account expires\s*:\s*1970-01-02\s*$",
                        self.run(("/usr/bin/chage", "--list", "--iso8601", ACCOUNT)),
                    ),
                    "rollback_expiry",
                )
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        # Revoke only newly published access artifacts with exact ownership/pins.
        # Preserve account, encrypted token, safe receipts and replay history.
        revoke = (
            SUDOERS,
            LIBEXEC / "api-quota-broker-control",
            LIBEXEC / "api-quota-broker-ops-client",
            SYSTEM / "api-quota-broker-ops.socket",
            SYSTEM / "api-quota-broker-ops@.service",
            TMPFILES,
        )
        for p in revoke:
            if p in self.published and not path_absent(p):
                try:
                    raw = self.e.read_root(p, sha=self.published[p])
                    require(
                        hashlib.sha256(raw).hexdigest() == self.published[p],
                        "rollback_artifact_drift",
                    )
                    p.unlink()
                except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                    ok = False
        if self.daemon_changed:
            try:
                self.run(("/usr/bin/systemctl", "daemon-reload"))
                self.run(("/usr/sbin/visudo", "-c"))
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        if hasattr(self, "baseline"):
            try:
                self.preservation()
            except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
                ok = False
        if self.ssh_preexisting:
            self.owned_rollback_verified = ok
            # Existing rule is never owned by this transaction, even on failure.
            try:
                require(
                    not self.ssh_write_intent and not self.ssh_written, "recovery_state_changed"
                )
                self.verify_recovery_pin()
                if hasattr(self, "ssh_hashes"):
                    require(self.ssh_source() == self.ssh_hashes, "recovery_state_changed")
                    for user in (ACCOUNT, "morris"):
                        require(
                            self.effective_ssh(user) == self.ssh_before[user],
                            "recovery_state_changed",
                        )
                    self.verify_ssh_reload()
                if hasattr(self, "recovery_source_fingerprint"):
                    require(
                        self.recovery_sources() == self.recovery_source_fingerprint,
                        "recovery_source_changed",
                    )
            except BaseException:  # noqa: BLE001 - recovery never takes existing leaf ownership.
                ok = False
            # We preserve the intended disk rule, not restore unknown original
            # daemon memory. Even a verified reload followed by failure is not
            # a full restoration of this transaction's starting live state.
            return ok and not self.ssh_reload_attempted
        if self.ssh_write_intent:
            # Keep the deny rule when account locking/revocation was uncertain.
            # Never remove a preexisting or drifted SSH fragment.
            if not ok:
                return False
            try:
                self.e.root_dir(SSH_DENY.parent)
                try:
                    SSH_DENY.lstat()
                except FileNotFoundError:
                    require(
                        not self.ssh_written and not self.ssh_reload_attempted, "ssh_source_changed"
                    )
                    require(self.ssh_source() == self.ssh_hashes, "ssh_source_changed")
                    for user in (ACCOUNT, "morris"):
                        require(
                            self.effective_ssh(user) == self.ssh_before[user], "ssh_source_changed"
                        )
                    self.verify_ssh_reload()
                    return ok
                self.e.read_root(
                    SSH_DENY, sha=hashlib.sha256(SSH_DENY_BYTES).hexdigest(), mode=0o644
                )
                hashes = self.ssh_source()
                require(
                    {k: v for k, v in hashes.items() if k != str(SSH_DENY)} == self.ssh_hashes,
                    "ssh_source_changed",
                )
                for user in (ACCOUNT, "morris"):
                    ssh_candidate_equal(self.ssh_before[user], self.effective_ssh(user))
                SSH_DENY.unlink()
                fd = os.open(SSH_DENY.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.run(("/usr/sbin/sshd", "-t"))
                if self.ssh_reload_attempted:
                    self.run(("/usr/bin/systemctl", "reload", "ssh.service"))
                    self.verify_ssh_reload()
                require(self.ssh_source() == self.ssh_hashes, "ssh_source_changed")
                for user in (ACCOUNT, "morris"):
                    require(self.effective_ssh(user) == self.ssh_before[user], "ssh_source_changed")
            except BaseException:  # noqa: BLE001 - never expose root configuration.
                ok = False
        return ok

    def receipt(self, result):
        if self.receipt_dir is not None:
            p = self.receipt_dir / ("receipt-" + os.urandom(8).hex() + ".json")
            self.e.write_exclusive(p, json.dumps(result, sort_keys=True).encode() + b"\n")


def main():
    if len(sys.argv) == 1:
        print(
            json.dumps(
                {
                    "mode": "asus_ops_complete_bootstrap_candidate",
                    "apply": False,
                    "source_password_file_used": False,
                    "token_days_max": 30,
                    "native_restart_executed": False,
                    "host_changes": 0,
                    "provider_calls": 0,
                    "ssh_change_requires_explicit_authorization": True,
                }
            )
        )
        return 0
    try:
        require(
            sys.argv[1:]
            in (
                ["--apply"],
                ["--apply-with-ssh-deny"],
                ["--continue-pinned-ssh"],
                ["--diagnose-retained-inspect"],
                ["--restore-agent-inspect"],
            ),
            "arguments_denied",
        )
        source, entry = sealed_sources()

        def interrupted(signum, frame):
            raise BootstrapStop(
                "bootstrap_termination_requested"
                if signum == signal.SIGTERM
                else "bootstrap_budget_insufficient"
            )

        old_term = signal.signal(signal.SIGTERM, interrupted)
        old_alarm = signal.signal(signal.SIGALRM, interrupted)
        try:
            result = initialize(
                NativeBootstrap(
                    source,
                    entry,
                    ssh_change_authorized=sys.argv[1:]
                    in (["--apply-with-ssh-deny"], ["--continue-pinned-ssh"]),
                    retained_diagnosis=sys.argv[1:] == ["--diagnose-retained-inspect"],
                    retained_agent=sys.argv[1:] == ["--restore-agent-inspect"],
                    continue_pinned_ssh=sys.argv[1:] == ["--continue-pinned-ssh"],
                )
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGALRM, old_alarm)
    except BaseException:  # noqa: BLE001 - rollback/redaction includes interruption.
        result = {"status": "blocked", "code": "bootstrap_unverified", "automatic_retry": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
