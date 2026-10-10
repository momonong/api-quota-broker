"""Fixed root-only ops receipt projection; default reads nothing.

--command emits a complete normal-sudo readonly command for the ASUS TTY.
Never reads token/password/ciphertext/journal or changes files/services.
"""

import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path

BASE = Path("/var/backups/api-quota-broker")
MAX_RECEIPTS = 128
MAX_ENTRIES = 512
MAX_BYTES = 16384
CODES = frozenset(
    [
        "account_exists",
        "account_expiry_active",
        "account_identity",
        "activation_unverified",
        "bootstrap_budget_insufficient",
        "bootstrap_budget_unverified",
        "bootstrap_termination_requested",
        "bootstrap_unverified",
        "chage_interface_unverified",
        "core_limit",
        "core_limit_set_failed",
        "core_limit_query_failed",
        "credential_interface_unverified",
        "credential_tool_unverified",
        "cron_group_unverified",
        "cron_job_path_unverified",
        "cron_metadata_unverified",
        "cron_schedule_unverified",
        "directory_mode_untrusted",
        "dumpability",
        "effective_sudo_policy_unverified",
        "expiry_invalid",
        "file_mode_untrusted",
        "home_created",
        "host_identity",
        "host_key_metadata_unverified",
        "human_scope_attestation",
        "initialization_unverified",
        "memory_limit",
        "native_failed",
        "native_inspect",
        "native_os_failure",
        "native_tool_unverified",
        "native_tty_required",
        "ops_artifact_exists",
        "ops_home_exists",
        "ops_job_exists",
        "ops_unit_exists",
        "password_inactive",
        "peer_identity",
        "pin_changed",
        "policy_drift",
        "proposal_syntax_unverified",
        "release_changed",
        "release_link_untrusted",
        "release_path_untrusted",
        "required_path_inaccessible",
        "required_path_missing",
        "rollback_budget_exhausted",
        "root_directory_untrusted",
        "root_file_changed",
        "root_file_untrusted",
        "runtime_file_type",
        "runtime_link_untrusted",
        "runtime_mutable",
        "service_unhealthy",
        "services_changed",
        "ssh_authentication_not_isolated",
        "ssh_authorizedkeyscommand_not_isolated",
        "ssh_authorizedkeysfile_not_isolated",
        "ssh_change_authorization_required",
        "ssh_deny_list_changed",
        "ssh_deny_not_effective",
        "ssh_fragment_exists",
        "ssh_gssapiauthentication_not_isolated",
        "ssh_hostbasedauthentication_not_isolated",
        "ssh_kbdinteractiveauthentication_not_isolated",
        "ssh_other_settings_changed",
        "ssh_output_unverified",
        "ssh_passwordauthentication_not_isolated",
        "ssh_reload_unverified",
        "ssh_scope_unverified",
        "ssh_source_changed",
        "ssh_trustedusercakeys_not_isolated",
        "sudo_privilege_transition_unavailable",
        "sudo_version",
        "swap_limit",
        "token_format",
        "unauthorized_or_cached_access",
        "unit_dropins_changed",
        "unit_scope",
        "worker_cleanup_unverified",
        "wrong_password_accepted",
    ]
)
CHECKS = frozenset(
    [
        "artifact_absence",
        "bootstrap_guard",
        "broker_pins",
        "credential_tool",
        "gateway_metadata",
        "home_absence",
        "host_key",
        "identity",
        "job_absence",
        "native_tools",
        "parent_metadata",
        "proposal_syntax",
        "receipt_storage",
        "service_baseline",
        "socket_absence",
        "ssh_candidate",
        "ssh_policy",
        "ssh_reload_interface",
        "sudo_version",
        "sudoers_syntax",
        "template_absence",
    ]
)
CODES |= {"recovery_pin_unverified", "recovery_state_changed", "recovery_source_changed"}
CODES |= {"tty_multiline", "tty_envelope_unverified", "tty_eof", "tty_bound", "tty_noecho_failed"}
CHECKS |= {"recovery_pin", "retained_state"}
RECOVERY_STEPS = frozenset(
    (
        "pin",
        "ssh_source",
        "ssh_effective",
        "source_fingerprint",
        "ssh_denial",
        "ssh_syntax",
        "ssh_reload_interface",
    )
)
CODES |= {
    "recovery_dependency_unverified",
    "recovery_ssh_source_unverified",
    "recovery_ssh_effective_unverified",
    "recovery_fingerprint_unverified",
    "recovery_syntax_unverified",
}
CODES |= {
    "native_exit",
    "native_timeout",
    "native_output_bound",
    "native_diagnostic_blocked",
    "retained_account_unverified",
    "retained_state_unverified",
    "retained_claims_present",
    "retained_policy_unverified",
    "retained_credential_unverified",
    "retained_file_changed",
    "retained_cleanup_unverified",
}
STAGES = frozenset(
    (
        "preflight",
        "memory_guard",
        "ssh_account_isolation",
        "human_scope_and_token",
        "fresh_doppler",
        "account_create",
        "account_password",
        "sealed_publish",
        "native_authorization_checks",
        "native_socket_inspect",
        "activation",
        "preservation_verify",
        "retained_recheck",
        "retained_claim",
        "retained_publish",
        "retained_account_unlock",
        "retained_cleanup",
    )
)
MODES = frozenset(
    (
        "asus_ops_socket_bootstrap",
        "asus_ops_socket_initialized",
        "asus_ops_socket_initialization",
        "asus_ops_socket_bootstrap_progress",
        "asus_ops_native_diagnostic",
        "asus_ops_retained_diagnosis",
        "asus_ops_agent_inspect_ready",
    )
)
PROGRESS = frozenset(
    (
        "ssh_write_intent",
        "ssh_file_written",
        "ssh_reload_intent",
        "ssh_reload_verified",
        "ssh_preexisting_reload_intent",
        "ssh_preexisting_reload_verified",
    )
)
BOOLEANS = (
    "rollback_verified",
    "manual_recovery_required",
    "automatic_retry",
    "native_restart_executed",
    "ssh_change_needed",
    "native_inspect_verified",
    "ssh_rule_added",
    "ssh_preexisting",
    "ssh_preexisting_retained",
    "owned_artifact_rollback_verified",
    "ssh_reload_attempted",
    "ssh_reload_verified",
    "original_live_ssh_state_restored",
    "retained_contract_restored",
    "public_bytes_restored",
    "public_metadata_restored",
    "cipher_metadata_preserved",
    "policy_bytes_preserved",
    "attempt_claim_retained",
    "diagnosis_only",
    "agent_inspect_ready",
    "restart_enabled",
    "agent_password_prompt",
    "policy_expiry_not_extended",
    "os_account_expiry_bounded",
    "r13_claim_preserved",
    "activation_claim_retained",
    "credential_reused",
    "credential_reuse_selected",
    "credential_consumption_unverified",
    "account_locked_expired",
    "native_diagnostic_unverified",
)


def need(ok):
    if not ok:
        raise ValueError("ops_receipt_projection_unverified")


def trusted_dir(path, *, private=False):
    s = path.lstat()
    need(stat.S_ISDIR(s.st_mode) and s.st_uid == s.st_gid == 0 and not s.st_mode & 0o022)
    if private:
        need(stat.S_IMODE(s.st_mode) == 0o700)


NATIVE_STAGES = frozenset(
    (
        "client_connect",
        "client_identity",
        "client_response",
        "client_socket",
        "entry",
        "helper_guard",
        "helper_identity",
        "helper_operation",
        "helper_package",
        "helper_pins",
        "helper_policy",
        "helper_request",
        "helper_sudo_identity",
        "native_client",
        "native_socket_start",
        "native_worker_cleanup",
        "worker_cleanup",
        "worker_credential",
        "worker_doppler",
        "worker_guard",
        "worker_identity",
        "worker_operation",
        "worker_package",
        "worker_peer",
        "worker_policy",
        "worker_prepare",
        "worker_probe",
        "worker_request",
        "worker_session",
    )
)
NATIVE_CODES = frozenset(
    (
        "arguments_denied",
        "authentication_bypass_or_probe_failure",
        "authentication_failed",
        "claim_storage_limit",
        "client_denied",
        "core_limit",
        "core_limit_set_failed",
        "core_limit_query_failed",
        "credential_acl",
        "credential_format",
        "credential_metadata",
        "credential_read_denied",
        "credential_scope",
        "diagnostic_unverified",
        "directory_mode_untrusted",
        "doppler_auth_denied",
        "doppler_body_bound",
        "doppler_endpoint",
        "doppler_rate_limited",
        "doppler_response_unverified",
        "doppler_unavailable",
        "dumpability",
        "duplicate_json",
        "file_mode_untrusted",
        "helper_identity",
        "identity_changed",
        "invalid_json",
        "memory_limit",
        "native_exit",
        "native_failed",
        "native_os_failure",
        "native_output_bound",
        "native_timeout",
        "operation_denied",
        "operation_lock_untrusted",
        "operation_unknown",
        "package_digest",
        "package_files",
        "package_loader",
        "package_schema",
        "path_denied",
        "peer_denied",
        "pin_changed",
        "pipe_bound",
        "pipe_timeout",
        "policy_fields",
        "policy_values",
        "release_changed",
        "release_link_untrusted",
        "release_path_untrusted",
        "request_bound",
        "request_denied",
        "request_fields",
        "request_id_invalid",
        "required_path_inaccessible",
        "required_path_missing",
        "response_bound",
        "response_unverified",
        "restart_not_enabled",
        "restart_unverified",
        "root_directory_untrusted",
        "root_file_changed",
        "root_file_untrusted",
        "runtime_file_type",
        "runtime_link_untrusted",
        "runtime_mutable",
        "service_denied",
        "service_unhealthy",
        "socket_type",
        "socket_untrusted",
        "state_unverified",
        "sudo_identity",
        "sudo_privilege_transition_unavailable",
        "swap_limit",
        "tls_key_logging_denied",
        "token_expired",
        "unit_dropins_changed",
        "unit_scope",
        "unverified_exception",
        "worker_cleanup_unverified",
        "worker_identity",
    )
)
NATIVE_FIELDS = frozenset(
    (
        "automatic_retry",
        "cleanup_unverified",
        "code",
        "operation",
        "operation_may_have_completed",
        "rc",
        "request_id",
        "service",
        "stage",
        "status",
    )
)


def bounded_entries(path):
    for count, entry in enumerate(path.iterdir(), 1):
        need(count <= MAX_ENTRIES)
        yield entry


def receipt_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == before.st_gid == 0
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_nlink == 1
            and before.st_size <= MAX_BYTES
        )
        raw = f.read(MAX_BYTES + 1)
        after = os.fstat(f.fileno())
        need(
            len(raw) == before.st_size
            and (
                before.st_dev,
                before.st_ino,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_size,
            )
            == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size)
        )
    return raw, before.st_mtime_ns


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result)
            result[key] = value
        return result

    data = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: need(False))
    need(type(data) is dict)
    return data


def native_fields(value):
    need(type(value) is dict and set(value) == NATIVE_FIELDS)
    need(
        value["status"] == "blocked"
        and type(value["stage"]) is str
        and value["stage"] in NATIVE_STAGES
        and type(value["code"]) is str
        and value["code"] in NATIVE_CODES
        and value["service"] == "api-quota-broker.service"
        and value["automatic_retry"] is False
        and type(value["operation_may_have_completed"]) is bool
        and type(value["cleanup_unverified"]) is bool
        and (value["rc"] is None or type(value["rc"]) is int and -64 <= value["rc"] <= 255)
    )
    need(
        (value["operation"] is None and value["request_id"] is None)
        or (
            value["operation"] in ("inspect", "restart")
            and type(value["request_id"]) is str
            and re.fullmatch("[a-f0-9]{32}", value["request_id"])
        )
    )
    return dict(value)


def safe_fields(data):
    need(type(data) is dict)
    result = {}
    if type(data.get("recovery_step")) is str and data["recovery_step"] in RECOVERY_STEPS:
        result["recovery_step"] = data["recovery_step"]
    if type(data.get("rc")) is int and -64 <= data["rc"] <= 255:
        result["rc"] = data["rc"]
    if type(data.get("native_client_rc")) is int and -64 <= data["native_client_rc"] <= 255:
        result["native_client_rc"] = data["native_client_rc"]
    if "native_diagnostic" in data:
        try:
            result["native_diagnostic"] = native_fields(data["native_diagnostic"])
        except Exception:  # noqa: BLE001 - raw malformed fields never leave projector.
            result["native_diagnostic_unverified"] = True
    for key, allowed in (
        ("status", {"prepared", "passed", "blocked"}),
        ("mode", MODES),
        ("stage", STAGES),
        ("check", CHECKS),
        ("code", CODES),
        ("checkpoint", PROGRESS),
    ):
        value = data.get(key)
        if type(value) is str and value in allowed:
            result[key] = value
    for key in BOOLEANS:
        if type(data.get(key)) is bool:
            result[key] = data[key]
    if type(data.get("provider_calls")) is int and data["provider_calls"] == 0:
        result["provider_calls"] = 0
    if type(data.get("preflight_checks_passed")) is list:
        values = data["preflight_checks_passed"]
        need(len(values) <= len(CHECKS))
        result["preflight_checks_passed"] = [v for v in values if type(v) is str and v in CHECKS]
    failure = data.get("recovery_source_failure")
    if type(failure) is dict and failure.get("predicate") in {
        "source_set",
        "file_metadata",
        "mtime_fence",
        "ctime_fence",
        "include_directory_time",
        "helper_effective_path",
    }:
        projected = {"predicate": failure["predicate"]}
        for key, allowed in (
            ("field", {"sshdsessionpath", "sshdauthpath"}),
            ("user", {"broker-deploy", "morris"}),
            ("actual", {"missing", "different"}),
            (
                "path",
                {
                    "/etc/ssh/sshd_config",
                    "/etc/ssh/sshd_config.d/50-cloud-init.conf",
                    "/etc/ssh/sshd_config.d",
                    "/etc/default/ssh",
                    "/usr/lib/systemd/system/ssh.service",
                    "/usr/sbin/sshd",
                    "/usr/lib/openssh/sshd-session",
                    "/usr/lib/openssh/sshd-auth",
                },
            ),
        ):
            value = failure.get(key)
            if type(value) is str and value in allowed:
                projected[key] = value
        result["recovery_source_failure"] = projected
    return result


def collect():
    need(os.geteuid() == 0 and os.uname().nodename == "asus-ubuntu2604-server")
    for path in reversed((BASE, *BASE.parents)):
        trusted_dir(path)
    rows = []
    for folder in bounded_entries(BASE):
        if not re.fullmatch(r"ops-bootstrap-[a-f0-9]{32}", folder.name):
            continue
        trusted_dir(folder, private=True)
        for path in bounded_entries(folder):
            if not re.fullmatch(r"receipt-[a-f0-9]{16}\.json", path.name):
                continue
            raw, mtime = receipt_bytes(path)
            row = safe_fields(strict_json(raw))
            row.update(bootstrap_id=folder.name, receipt_id=path.name, mtime_ns=str(mtime))
            rows.append(row)
            need(len(rows) <= MAX_RECEIPTS)
    return {
        "status": "projected",
        "scope": "ops_bootstrap_receipts_only",
        "receipts": sorted(rows, key=lambda row: int(row["mtime_ns"])),
        "secret_artifacts_read": False,
        "service_mutations": 0,
    }


def normal_sudo_command(source):
    compile(source, "<fixed-ops-receipt-projection>", "exec")
    return "sudo -- /usr/bin/python3.14 -I -B -S -c " + shlex.quote(source) + " --read-only"


def main(argv):
    if argv == []:
        print(json.dumps({"mode": "ops_receipt_projection", "reads": 0, "applies": False}))
        return 0
    if argv == ["--command"]:
        print(normal_sudo_command(Path(__file__).read_text()))
        return 0
    try:
        need(argv == ["--read-only"])
        result = collect()
    except BaseException:  # noqa: BLE001 - fixed output also covers interrupted private reads.
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "ops_receipt_projection_unverified",
                    "raw_output_shown": False,
                }
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
