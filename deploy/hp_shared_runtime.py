"""Non-root HP shared Broker launcher. No Doppler, sudo, or deployment actions.

Systemd supplies an encrypted, instance-bound credential snapshot. An installer
must create the private state and empty ledger explicitly; startup never replaces
a missing ledger. Metadata imports retain their original observation times.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import resource
import signal
import stat
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = Path("/opt/quota-shared-broker")
STATE = Path("/var/lib/quota-shared-broker")
RUN = Path("/run/quota-shared-broker")
CREDENTIAL = Path("/run/credentials/quota-shared-broker.service/runtime.json")
PRIVATE_CONFIG = Path("/etc/quota-shared-broker/private-api.json")
INSTANCE = "hp-shared-20261111"
PROVIDER_REFS = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "ocrspace": "OCRSPACE_API_KEY",
    "sambanova": "SAMBANOVA_API_KEY",
    "huggingface": "HUGGINGFACE_API_TOKEN",
    "cerebras": "CEREBRAS_API_KEY",
}
LEGACY_PROVIDER_NAMES = frozenset({"OPENAI_API_KEY", "OPENROUTER_API_KEY"})
PROVIDER_NAMES = frozenset(PROVIDER_REFS.values()) | {"CLOUDFLARE_ACCOUNT_ID"}


def openrouter_metadata_get(key):
    """One fixed read-only request; no redirects, proxy, retry, or inference."""
    from quota_broker.bounded_curl import _quote, metrics_template, parse_curl_result, run_curl

    need(
        not any(k.lower() in {"http_proxy", "https_proxy", "all_proxy"} for k in os.environ),
        "proxy_environment",
    )
    options = [
        'url = "https://openrouter.ai/api/v1/key"',
        'request = "GET"',
        'proto = "=https"',
        'proto-redir = "=https"',
        'retry = "0"',
        'max-redirs = "0"',
        'connect-timeout = "10"',
        'max-time = "20"',
        "include",
        "suppress-connect-headers",
        'header = "Accept: application/json"',
        "header = " + _quote("Authorization: Bearer " + key),
        "write-out = " + _quote(metrics_template()),
    ]
    code, raw = run_curl(("\n".join(options) + "\n").encode(), 20, output_bound=81920)
    status, _, body, _ = parse_curl_result(code, raw, 23, response_bound=65536)
    need(status == 200, "metadata_request_rejected")
    return json.loads(body, object_pairs_hook=unique)


def refresh_openrouter(gateway, resolve, *, fetch=openrouter_metadata_get, now=None):
    """Fresh authoritative remaining count; original configuration expiry stays fixed."""
    current = now or datetime.now(UTC)
    targets = [t for t in gateway.targets if t.enabled and t.provider == "openrouter"]
    if not targets:
        return
    response = fetch(resolve("OPENROUTER_API_KEY"))
    need(isinstance(response, dict) and not response.get("error"), "metadata_schema")
    data = response.get("data")
    need(isinstance(data, dict) and data.get("is_free_tier") is True, "free_tier_unverified")
    daily = data.get("free_model_daily_requests")
    need(
        isinstance(daily, dict)
        and set(daily) == {"used", "remaining", "limit"}
        and all(type(v) is int and 0 <= v <= 10**9 for v in daily.values())
        and daily["limit"] == 50
        and daily["remaining"] == max(0, daily["limit"] - daily["used"]),
        "daily_counter_unknown",
    )
    remaining = data.get("limit_remaining")
    need(
        remaining is None or type(remaining) in {int, float} and remaining >= 0,
        "credit_gate_unknown",
    )
    reset = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for target in targets:
        gateway.observe_quota(
            target.id,
            {
                "metric": "requests",
                "window": "day",
                "remaining": daily["remaining"],
                "limit": daily["limit"],
                "as_of": current.isoformat(),
                "valid_until": min(current + timedelta(minutes=15), reset).isoformat(),
                "reset_at": reset.isoformat(),
                "provenance": "official",
                "source": "adapter:openrouter_key_account",
                "confidence": "provider_reported",
            },
        )


def refresh_loop(gateway, resolve, stopped, state):
    # A failed or refused query is not retried on a timer or after a restart.
    # Keep the last observation and its original expiry; the route then closes.
    marker = state / "metadata-refresh-stopped.json"
    if marker.exists():
        return
    while not stopped.is_set():
        try:
            refresh_openrouter(gateway, resolve)
        except Exception:  # noqa: BLE001 - persist only fixed status, never provider bodies
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(
                    {
                        "status": "stopped",
                        "provider": "openrouter",
                        "automatic_retry": False,
                        "at": datetime.now(UTC).isoformat(),
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            return
        if stopped.wait(300):
            return


def need(ok, code):
    if not ok:
        raise ValueError(code)


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate_json")
        result[key] = value
    return result


def read_json(path, maximum=262144, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= maximum,
            "unsafe_file",
        )
        need(not before.st_mode & 0o022, "writable_file")
        if private:
            # systemd credentials may be root:root 0440 with a service UID ACL.
            # The fixed root-owned directory, not owner-only mode, is the boundary.
            need(
                before.st_uid in {0, os.geteuid()} and not before.st_mode & 0o007,
                "unsafe_credential",
            )
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        need(
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and len(raw) == before.st_size,
            "changed_file",
        )
    return json.loads(raw, object_pairs_hook=unique)


def enabled_provider_names(targets):
    names = set()
    for target in targets:
        if not target.enabled:
            continue
        need(
            target.provider in PROVIDER_REFS
            and target.secret_ref == PROVIDER_REFS[target.provider]
            and target.account_id_ref
            == ("CLOUDFLARE_ACCOUNT_ID" if target.provider == "cloudflare" else None)
            and target.required_capacity
            and (target.provider != "cloudflare" or ("neurons", "day") in target.required_capacity),
            "enabled_target_requires_capacity",
        )
        names.add(target.secret_ref)
        if target.account_id_ref:
            names.add(target.account_id_ref)
    return frozenset(names)


def snapshot_scope(names, required_names):
    # Only the original fixed two-key payload retains its historical extra key.
    return names == required_names or (
        names == LEGACY_PROVIDER_NAMES and required_names <= LEGACY_PROVIDER_NAMES
    )


def credential_values(value, instance_id, *, required_names=LEGACY_PROVIDER_NAMES):
    need(
        isinstance(value, dict)
        and set(value)
        == {"schema", "instance_id", "digest_key", "queue_key", "legacy_token", "provider_keys"}
        and value["schema"] == 1
        and value["instance_id"] == instance_id,
        "credential_instance",
    )
    keys = value["provider_keys"]
    need(isinstance(keys, dict) and snapshot_scope(set(keys), required_names), "credential_scope")
    need(
        all(
            isinstance(v, str)
            and 8 <= len(v) <= 1024
            and v.isascii()
            and not any(c.isspace() for c in v)
            for v in keys.values()
        ),
        "credential_format",
    )
    digest_key = base64.b64decode(value["digest_key"], validate=True)
    queue_key = base64.b64decode(value["queue_key"], validate=True)
    need(32 <= len(digest_key) <= 64 and len(queue_key) == 32, "credential_key_length")
    legacy = value["legacy_token"]
    need(
        isinstance(legacy, str) and re.fullmatch(r"[A-Za-z0-9_~-]{32,256}", legacy),
        "credential_token",
    )
    snapshot = {name: keys[name] for name in required_names}

    def resolve(name):
        need(name in snapshot, "credential_reference_not_authorized")
        return snapshot[name]

    return resolve, digest_key, queue_key, legacy


def credential_fingerprint(digest_key, queue_key, instance_id):
    """Private instance binding; never printed or exposed by metadata endpoints."""
    material = b"quota-shared-ledger-binding-v1\x00"
    for value in (instance_id.encode("ascii"), digest_key, queue_key):
        material += len(value).to_bytes(4, "big") + value
    return hashlib.sha256(material).hexdigest()


def check_state(directory, instance_id, fingerprint=None):
    info = directory.lstat()
    need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "state_permissions",
    )
    marker = read_json(directory / "instance.json", private=True)
    need(
        set(marker)
        == {"schema", "instance_id", "historical_gap", "history_start"}
        | ({"credential_fingerprint"} if fingerprint is not None else set())
        and marker["schema"] == 1
        and marker["instance_id"] == instance_id
        and marker["historical_gap"] is True,
        "state_instance",
    )
    if fingerprint is not None:
        need(
            isinstance(marker["credential_fingerprint"], str)
            and hmac.compare_digest(marker["credential_fingerprint"], fingerprint),
            "state_credential_binding",
        )
    path = directory / "ledger.sqlite3"
    need(path.exists(), "ledger_missing")
    info = path.lstat()
    need(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600,
        "ledger_permissions",
    )
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = directory / ("ledger.sqlite3" + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            info = sidecar.lstat()
            need(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600,
                "ledger_permissions",
            )
    return path


def verify_tree(directory, *, owner=0, maximum=10000):
    """Reject replaceable/symlinked code before adding it to Python's import path."""
    pending, count = [directory], 0
    while pending:
        path = pending.pop()
        info = path.lstat()
        count += 1
        need(count <= maximum, "release_size")
        need(
            info.st_uid == owner
            and not info.st_mode & 0o022
            and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            and (not stat.S_ISREG(info.st_mode) or info.st_nlink == 1),
            "release_permissions",
        )
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())


def fixed_directory(path, *, owner, mode=None):
    info = path.lstat()
    need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == owner
        and not info.st_mode & 0o022
        and (mode is None or stat.S_IMODE(info.st_mode) == mode),
        "directory_permissions",
    )


def secret_inventory(raw, temporary, *, required_names=LEGACY_PROVIDER_NAMES):
    from quota_broker.config import load_secret_inventory

    need(
        isinstance(raw, dict)
        and isinstance(raw.get("names"), list)
        and all(isinstance(name, str) for name in raw["names"])
        and len(set(raw["names"])) == len(raw["names"])
        and snapshot_scope(set(raw["names"]), required_names),
        "inventory_scope",
    )
    need(raw.get("project") == "api-quota-broker" and raw.get("config") == "dev", "inventory_scope")
    path = temporary / "secret-inventory.json"
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(raw, stream)
    return load_secret_inventory(path, "api-quota-broker", "dev")


def listener_settings(host, metadata_port, private_port):
    need(
        host == "127.0.0.1"
        and type(metadata_port) is int
        and type(private_port) is int
        and 1 <= metadata_port <= 65535
        and 1 <= private_port <= 65535
        and metadata_port != private_port,
        "private_listener_required",
    )
    return metadata_port, private_port


def import_observations(gateway, rows):
    need(isinstance(rows, list) and len(rows) <= 128, "observation_count")
    seen = set()
    for row in rows:
        need(
            isinstance(row, dict) and set(row) == {"target_id", "observation"}, "observation_fields"
        )
        observation = row["observation"]
        need(isinstance(observation, dict), "observation_fields")
        identity = (row["target_id"], observation.get("metric"), observation.get("window"))
        need(identity not in seen, "observation_duplicate")
        seen.add(identity)
        gateway.observe_quota(row["target_id"], observation)


def sensitive_policy(base, overlay):
    """Root-managed additions cannot replace or relax published project policy."""
    need(
        isinstance(overlay, dict)
        and set(overlay) == {"version", "principals"}
        and overlay["version"] == 1
        and isinstance(overlay["principals"], list)
        and 0 < len(overlay["principals"]) <= 256,
        "sensitive_policy_shape",
    )
    projects = {p["project_id"] for p in base["principals"]}
    digests = {p["token_sha256"] for p in base["principals"]}
    for principal in overlay["principals"]:
        need(
            isinstance(principal, dict)
            and principal.get("project_id")
            and principal["project_id"] not in projects
            and principal.get("token_sha256") not in digests
            and principal.get("allowed_execution") == ["local_only"]
            and principal.get("default_execution") == "local_only"
            and principal.get("encryption_required") is True,
            "sensitive_policy_scope",
        )
        digests.add(principal["token_sha256"])
    return {"version": 1, "principals": [*base["principals"], *overlay["principals"]]}


def copy_worker_credential(source, destination):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid in {0, os.geteuid()}
            and not info.st_mode & 0o007
            and not info.st_mode & 0o022
            and 32 <= info.st_size <= 4096,
            "worker_credential_metadata",
        )
        raw = stream.read(4097).strip()
    need(
        32 <= len(raw) <= 4096 and raw.isascii() and not any(chr(c).isspace() for c in raw),
        "worker_credential_value",
    )
    fd, name = tempfile.mkstemp(prefix=".worker-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def run_runtime(release, state, runtime, credential, *, ports=(18587, 18588), private_config=None):
    from quota_broker.access import AccessPolicyFile, ProjectAccess
    from quota_broker.config import load_gateway_config
    from quota_broker.gateway import Gateway
    from quota_broker.gateway_server import make_gateway_server
    from quota_broker.queue import DurableQueue
    from quota_broker.registry import Registry

    need(os.geteuid() != 0, "service_identity_required")
    listener_settings("127.0.0.1", *ports)
    fixed_directory(runtime, owner=os.geteuid(), mode=0o700)
    registry = Registry.load(release / "config/registry.json")
    targets = load_gateway_config(release / "config/gateway.json", registry=registry)
    required_names = enabled_provider_names(targets)
    resolve, digest, queue_key, legacy = credential_values(
        read_json(credential, private=True), INSTANCE, required_names=required_names
    )
    db = check_state(state, INSTANCE, credential_fingerprint(digest, queue_key, INSTANCE))
    with tempfile.TemporaryDirectory(prefix="policy-", dir=runtime) as temporary:
        policy_path = Path(temporary) / "access.json"
        policy_data = read_json(release / "config/access.json", private=True)
        secure = None
        if private_config is not None:
            secure = read_json(private_config, private=True)
            need(
                isinstance(secure, dict)
                and set(secure) == {"encrypted", "local", "access", "worker_credential"}
                and all(isinstance(p, str) and Path(p).is_absolute() for p in secure.values())
                and len(set(secure.values())) == 4,
                "private_configuration_shape",
            )
            policy_data = sensitive_policy(
                policy_data, read_json(Path(secure["access"]), private=True)
            )
        policy_path.write_text(json.dumps(policy_data))
        policy_path.chmod(0o600)
        policy = AccessPolicyFile(policy_path)
        policy.read()
        inventory = secret_inventory(
            read_json(release / "config/secret-inventory.json"),
            Path(temporary),
            required_names=required_names,
        )
        need(
            all(
                not target.enabled
                or (target.expires_at is not None and target.expires_at <= inventory.expires_at)
                for target in targets
            ),
            "eligibility_exceeds_inventory_validity",
        )
        gateway = Gateway(
            db, targets, digest, resolve, registry=registry, secret_inventory=inventory
        )
        import_observations(gateway, read_json(release / "config/observations.json"))
        queue = DurableQueue(gateway, queue_key)
        endpoint, local = None, None
        if secure is not None:
            from quota_broker.encrypted_transport import EncryptedEndpoint
            from quota_broker.local_jobs import LocalHTTPBackend, LocalJobs

            endpoint = EncryptedEndpoint.load(secure["encrypted"])
            copy_worker_credential(Path(secure["worker_credential"]), runtime / "worker.token")
            backend = LocalHTTPBackend.load(secure["local"])
            need(backend.exact_lookup, "exact_worker_lookup_required")
            need(backend.token_file == runtime / "worker.token", "worker_credential_location")
            local = LocalJobs(queue, ProjectAccess(gateway, policy), backend)
        metadata = make_gateway_server(
            gateway,
            legacy,
            port=ports[0],
            queue=queue,
            worker=True,
            access_policy=policy,
            serialize_work=True,
            **({"local_jobs": local} if secure is not None else {}),
        )
        try:
            private = make_gateway_server(
                gateway,
                legacy,
                port=ports[1],
                queue=queue,
                access_policy=policy,
                private_api=True,
                serialize_work=True,
                **(
                    {"local_jobs": local, "encrypted_endpoint": endpoint}
                    if secure is not None
                    else {}
                ),
            )
        except BaseException:
            metadata.server_close()
            raise
        stopped = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        threads = [
            threading.Thread(target=s.serve_forever, daemon=True) for s in (metadata, private)
        ]
        if any(t.enabled and t.provider == "openrouter" for t in targets):
            threads.append(
                threading.Thread(
                    target=refresh_loop, args=(gateway, resolve, stopped, state), daemon=True
                )
            )
        for thread in threads:
            thread.start()
        print(
            json.dumps({"status": "ready", "instance": INSTANCE, "paid_allowed": False}), flush=True
        )
        try:
            stopped.wait()
        finally:
            # Stop claiming new jobs before closing either listener. A provider
            # call already dispatched must settle into this ledger before exit.
            metadata.stop_worker.set()
            for server in (private, metadata):
                server.shutdown()
                server.server_close()
            if metadata.worker_thread is not None:
                metadata.worker_thread.join()
            for thread in threads:
                thread.join(timeout=5)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    need(not sys.argv[1:], "fixed_arguments_required")
    for directory in (
        Path("/opt"),
        BASE,
        BASE / "releases",
        Path("/var"),
        STATE.parent,
        RUN.parent,
        CREDENTIAL.parent.parent,
    ):
        fixed_directory(directory, owner=0)
    fixed_directory(CREDENTIAL.parent, owner=0)
    link = BASE / "current"
    info = link.lstat()
    need(stat.S_ISLNK(info.st_mode) and info.st_uid == 0, "release_current")
    relative = os.readlink(link)
    need(re.fullmatch(r"releases/[a-z0-9][a-z0-9_-]{0,95}", relative), "release_name")
    release = BASE / relative
    verify_tree(release)
    for directory in (Path("/opt/momonong-quota-readonly"),):
        fixed_directory(directory, owner=0)
    verify_tree(Path("/opt/momonong-quota-readonly/site"))
    sys.path[:0] = [str(release / "app"), "/opt/momonong-quota-readonly/site"]
    private_config = None
    if PRIVATE_CONFIG.exists() or PRIVATE_CONFIG.is_symlink():
        fixed_directory(PRIVATE_CONFIG.parent, owner=0)
        info = PRIVATE_CONFIG.lstat()
        need(
            stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            "private_configuration_owner",
        )
        private_config = PRIVATE_CONFIG
        verify_tree(Path("/opt/quota-private-crypto/site"))
        sys.path.insert(1, "/opt/quota-private-crypto/site")
    run_runtime(release, STATE, RUN, CREDENTIAL, private_config=private_config)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never expose private credential or provider errors
        print(
            '{"status":"stopped","reason":"runtime_initialization_or_execution_failed"}', flush=True
        )
        raise SystemExit(1) from None
