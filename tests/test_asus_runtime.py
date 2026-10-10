"""Offline fixtures exercise preparation, artifact trust and native runtime gates."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
SPEC = importlib.util.spec_from_file_location(
    "prepare_asus_runtime", ROOT / "scripts/prepare_asus_runtime.py"
)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def prohibited(*args, **kwargs):
    raise AssertionError("unapproved network/process/credential access")


def uv_archive(monkeypatch, *, extra=None, kind=None):
    output = io.BytesIO()
    prefix = runtime.UV_NAME.removesuffix(".tar.gz")
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, raw in [
            (prefix + "/uv", b"\x7fELFfixture-uv"),
            (prefix + "/uvx", b"\x7fELFfixture-uvx"),
        ]:
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            item.mode = 0o755
            archive.addfile(item, io.BytesIO(raw))
        if extra:
            item = tarfile.TarInfo(extra)
            if kind:
                item.type = kind
                item.linkname = "/credential"
            archive.addfile(item, io.BytesIO())
    raw = output.getvalue()
    monkeypatch.setattr(runtime, "UV_HASH", digest(raw))
    monkeypatch.setattr(runtime, "UV_SIZE", len(raw))
    return raw


def source_archive(tmp_path, *, modified_lock=None):
    tool = runtime._release_tool()
    payload = {name: b"VALUE = 1\n" for name in tool._paths()}
    payload["pyproject.toml"] = (ROOT / "pyproject.toml").read_bytes()
    payload["uv.lock"] = (
        modified_lock if modified_lock is not None else (ROOT / "uv.lock").read_bytes()
    )
    release = {
        "schema_version": 1,
        "policy": "asus-release-v1",
        "project": "api-quota-broker",
        "package_version": VERSION,
        "source": {
            "head": "a" * 40,
            "working_tree_dirty": False,
            "payload_dirty": False,
            "included_changes": [],
            "description": tool.DESCRIPTION,
        },
        "files": [
            {"path": p, "size": len(b), "sha256": digest(b), "mode": tool._mode(p)}
            for p, b in sorted(payload.items())
        ],
    }
    manifest = tool._canonical(release)
    raw = tool._tar(payload, manifest)
    path = tmp_path / "release.tar"
    path.write_bytes(raw)
    return path, digest(manifest), release


def project_wheel(release, *, extra=None, corrupt_source=False, metadata=None):
    prefix = f"api_quota_broker-{release['package_version']}.dist-info/"
    contents = {
        r["path"].removeprefix("src/"): b"VALUE = 1\n"
        for r in release["files"]
        if r["path"].startswith("src/quota_broker/")
    }
    if corrupt_source:
        contents["quota_broker/__init__.py"] = b"CHANGED = 1\n"
    contents[prefix + "METADATA"] = (
        metadata
        or f"Metadata-Version: 2.3\nName: api-quota-broker\nVersion: {release['package_version']}\nRequires-Python: >=3.12\nRequires-Dist: cryptography>=44.0.0\nRequires-Dist: pypdf>=6.0.0\n\n".encode()
    )
    contents[prefix + "WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
    contents[prefix + "entry_points.txt"] = (
        b"[console_scripts]\nquota-broker = quota_broker.cli:main\n"
    )
    if extra:
        contents[extra] = b"arbitrary"
    rows = [
        (
            name,
            "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="),
            str(len(raw)),
        )
        for name, raw in contents.items()
    ]
    rows.append((prefix + "RECORD", "", ""))
    record = io.StringIO()
    csv.writer(record).writerows(rows)
    contents[prefix + "RECORD"] = record.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, raw in contents.items():
            archive.writestr(name, raw)
    return output.getvalue()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    tool_raw = uv_archive(monkeypatch)
    original = runtime.RUNTIME
    bodies = {a.name: ("fixture-" + a.name).encode() for a in original}
    fixture_artifacts = tuple(
        a._replace(sha256=digest(bodies[a.name]), size=len(bodies[a.name])) for a in original
    )
    lock = (ROOT / "uv.lock").read_text()
    for before, after in zip(original, fixture_artifacts, strict=True):
        lock = lock.replace(before.sha256, after.sha256).replace(
            f"size = {before.size}", f"size = {after.size}"
        )
    monkeypatch.setattr(runtime, "RUNTIME", fixture_artifacts)
    source, source_hash, release = source_archive(tmp_path, modified_lock=lock.encode())
    monkeypatch.setattr(runtime, "_download_uv", lambda: tool_raw)
    monkeypatch.setattr(runtime, "_download", lambda artifact: bodies[artifact.name])
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: b"uv 0.9.5\n")
    monkeypatch.setattr(runtime, "_build", lambda *args, **kwargs: project_wheel(release))
    bundle = tmp_path / "runtime"
    receipt = runtime.prepare_runtime(source, source_hash, bundle)
    return bundle, receipt, source, source_hash, release


def test_default_plan_cannot_read_files_execute_or_fetch(monkeypatch, capsys):
    import socket

    class NoSocket(socket.socket):
        def __new__(cls, *args, **kwargs):
            return prohibited(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", NoSocket)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(os, "getenv", prohibited)
    monkeypatch.setattr(Path, "open", prohibited)
    assert runtime.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "dry_run" and plan["execution_authorized"] is False
    assert plan["artifact_count"] == 10 and plan["max_requests"] == 11
    assert len(plan["downloads"]) == 9 and plan["wheel_redirects"] is False
    assert plan["retry_count"] == 0 and plan["wheel_per_artifact_total_seconds"] == 20
    assert plan["uv_total_seconds"] == 300
    assert (
        plan["max_download_wall_seconds"] == plan["preparation"]["max_download_wall_seconds"] == 480
    )
    assert plan["uv_total_seconds"] + len(plan["downloads"]) * 20 == 480
    assert plan["tool_archive"]["redirect"]["host"] == "release-assets.githubusercontent.com"
    assert not plan["target_install"] and not plan["global_install"]


def test_selected_runtime_exactly_matches_repository_lock():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    for artifact in runtime.RUNTIME:
        assert artifact.url.startswith("https://files.pythonhosted.org/packages/")
        assert artifact.name.endswith(".whl")
        assert packages[artifact.package]["version"] == artifact.version
        assert any(
            w["url"] == artifact.url
            and w["hash"] == "sha256:" + artifact.sha256
            and w["size"] == artifact.size
            for w in packages[artifact.package]["wheels"]
        )
    assert {a.package for a in runtime.BUILD_TOOLS} == {
        "hatchling",
        "packaging",
        "pathspec",
        "pluggy",
        "trove-classifiers",
    }


def test_fixed_hatchling_timestamp_is_a_valid_zip_timestamp():
    from datetime import UTC, datetime

    instant = datetime.fromtimestamp(runtime.PREPARATION["source_date_epoch"], UTC)
    metadata = zipfile.ZipInfo("fixture", instant.timetuple()[:6])
    assert metadata.date_time == (2020, 2, 2, 0, 0, 0)


@pytest.mark.parametrize("mode", ["redirect", "corrupt", "size"])
def test_wheel_download_refuses_redirect_hash_and_size(mode, monkeypatch):
    artifact = runtime.RUNTIME[0]
    raw = b"fixture"
    artifact = artifact._replace(sha256=digest(raw), size=len(raw))
    monkeypatch.setattr(runtime, "RUNTIME", (artifact,))
    response = (
        b"fixture\nquota-runtime-status:302"
        if mode == "redirect"
        else b"changed\nquota-runtime-status:200"
        if mode == "corrupt"
        else b"fixture-more\nquota-runtime-status:200"
    )
    calls = []
    monkeypatch.setattr(runtime, "_run", lambda command, *args: calls.append(command) or response)
    with pytest.raises(runtime.RuntimeErrorSafe):
        runtime._download(artifact)
    assert len(calls) == 1 and "--location" not in calls[0] and "--disable" in calls[0]
    assert not any("Authorization" in arg for arg in calls[0])


def test_unlisted_download_never_executes(monkeypatch):
    monkeypatch.setattr(runtime, "_run", prohibited)
    with pytest.raises(runtime.RuntimeErrorSafe, match="runtime preparation failed"):
        runtime._download(runtime.RUNTIME[0]._replace(url="https://evil.invalid/artifact.whl"))


def test_uv_single_approved_redirect_and_exact_hash(monkeypatch):
    raw = uv_archive(monkeypatch)
    calls = []

    def curl(url, timeout, bound, **kwargs):
        calls.append((url, timeout, kwargs))
        if len(calls) == 1:
            return (
                b"HTTP/2 302\r\nLocation: https://release-assets.githubusercontent.com/fixed?public=signature\r\n\r\n",
                b"302",
            )
        return raw, b"200"

    monkeypatch.setattr(runtime, "_curl", curl)
    assert runtime._download_uv() == raw
    assert len(calls) == 2 and calls[0][0] == runtime.UV_URL
    assert calls[1][1] <= calls[0][1] == 300
    assert calls[1][2] == {"progress_phase": "uv_asset_download"}


@pytest.mark.parametrize("failed_stage", ["uv_origin_download", "uv_asset_download"])
def test_uv_deadline_preserves_safe_stage_without_retry(failed_stage, monkeypatch, capsys):
    calls = []

    def curl(url, timeout, bound, **kwargs):
        calls.append((url, timeout, kwargs))
        if failed_stage == "uv_origin_download" or len(calls) == 2:
            raise runtime.RuntimeErrorSafe("process_deadline")
        return (
            b"HTTP/2 302\r\nLocation: https://release-assets.githubusercontent.com/f?opaque=PRIVATE\r\n\r\n",
            b"302",
        )

    monkeypatch.setattr(runtime, "_curl", curl)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime._download_uv()
    assert caught.value.reason == "process_deadline"
    assert caught.value.phase == failed_stage
    assert len(calls) == (1 if failed_stage == "uv_origin_download" else 2)

    def failed(*args):
        raise caught.value

    monkeypatch.setattr(runtime, "prepare_runtime", failed)
    assert (
        runtime.main(
            ["prepare", "--archive", "unused", "--manifest-sha256", "unused", "--output", "unused"]
        )
        == 2
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {
        "schema_version": 1,
        "status": "failed",
        "phase": failed_stage,
        "reason": "process_deadline",
    }


@pytest.mark.parametrize(
    "location",
    [
        "http://release-assets.githubusercontent.com/f",
        "https://evil.invalid/f",
        "https://release-assets.githubusercontent.com.evil.invalid/f",
        "https://user@release-assets.githubusercontent.com/f",
        "https://release-assets.githubusercontent.com:443/f",
        "https://release-assets.githubusercontent.com/f#fragment",
        "https://release-assets.githubusercontent.com/f\nInjected: value",
    ],
)
def test_uv_unapproved_redirect_denied(location, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_curl",
        lambda *args, **kwargs: (f"HTTP/2 302\r\nLocation: {location}\r\n\r\n".encode(), b"302"),
    )
    with pytest.raises(runtime.RuntimeErrorSafe):
        runtime._download_uv()


@pytest.mark.parametrize("final_status", [b"301", b"302", b"307", b"403"])
def test_uv_redirect_chain_or_failed_final_status_stops(final_status, monkeypatch):
    calls = []

    def curl(*args, **kwargs):
        calls.append(args)
        return (
            (
                b"HTTP/2 302\r\nLocation: https://release-assets.githubusercontent.com/f\r\n\r\n",
                b"302",
            )
            if len(calls) == 1
            else (b"body", final_status)
        )

    monkeypatch.setattr(runtime, "_curl", curl)
    with pytest.raises(runtime.RuntimeErrorSafe):
        runtime._download_uv()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "extra,kind",
    [
        ("../uv", None),
        ("other/uv", None),
        ("uv-x86_64-unknown-linux-gnu/credential", None),
        ("uv-x86_64-unknown-linux-gnu/extra", tarfile.SYMTYPE),
        ("uv-x86_64-unknown-linux-gnu/extra", tarfile.LNKTYPE),
    ],
)
def test_tool_archive_member_whitelist(extra, kind, monkeypatch):
    raw = uv_archive(monkeypatch, extra=extra, kind=kind)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime._uv_binary(raw)
    assert caught.value.reason == "tool_archive_members"


def test_complete_fixture_receipt_binds_source_tools_wheels_and_requirements(prepared):
    bundle, receipt, _, source_hash, _ = prepared
    assert receipt["status"] == "verified"
    assert receipt["source_payload_manifest_sha256"] == source_hash
    assert runtime.verify_bundle(bundle, receipt["runtime_manifest_sha256"], source_hash) == receipt
    requirements = (bundle / "requirements.txt").read_text()
    assert requirements.count("--hash=sha256:") == 5
    assert f"api-quota-broker=={VERSION}" in requirements
    assert (bundle / "tools/uv").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    "change",
    [
        "wrong_source",
        "wrong_manifest",
        "wheel",
        "requirements",
        "tool",
        "extra",
        "extra_dir",
        "symlink",
        "mode",
    ],
)
def test_bundle_tampering_fails_closed(prepared, change):
    bundle, receipt, _, source_hash, _ = prepared
    runtime_hash = receipt["runtime_manifest_sha256"]
    if change == "wrong_source":
        source_hash = "b" * 64
    elif change == "wrong_manifest":
        runtime_hash = "b" * 64
    elif change == "wheel":
        next((bundle / "wheelhouse").iterdir()).write_bytes(b"corrupt")
    elif change == "requirements":
        (bundle / "requirements.txt").write_text("arbitrary-url")
    elif change == "tool":
        (bundle / "tools/uv").write_bytes(b"changed")
    elif change == "extra":
        (bundle / "credential").write_text("never-read")
    elif change == "extra_dir":
        (bundle / "extra-dir").mkdir()
    elif change == "symlink":
        (bundle / "linked-dir").symlink_to(bundle / "tools", target_is_directory=True)
    else:
        (bundle / "tools/uv").chmod(0o777)
    with pytest.raises(runtime.RuntimeErrorSafe):
        runtime.verify_bundle(bundle, runtime_hash, source_hash)


def test_prepare_refuses_existing_output_before_download(prepared, monkeypatch):
    bundle, _, source, source_hash, _ = prepared
    monkeypatch.setattr(runtime, "_download_uv", prohibited)
    with pytest.raises(FileExistsError):
        runtime.prepare_runtime(source, source_hash, bundle)


def test_source_hash_failure_never_downloads_or_creates_output(tmp_path, monkeypatch):
    source, _, _ = source_archive(tmp_path)
    monkeypatch.setattr(runtime, "_download_uv", prohibited)
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        runtime.prepare_runtime(source, "0" * 64, output)
    assert not output.exists()


def test_source_lock_failure_never_downloads(tmp_path, monkeypatch):
    raw = (ROOT / "uv.lock").read_text().replace(runtime.RUNTIME[0].sha256, "0" * 64)
    source, source_hash, _ = source_archive(tmp_path, modified_lock=raw.encode())
    monkeypatch.setattr(runtime, "_download_uv", prohibited)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime.prepare_runtime(source, source_hash, tmp_path / "output")
    assert caught.value.reason == "runtime_lock"


@pytest.mark.parametrize(
    "extra,corrupt,metadata",
    [
        ("../credential", False, None),
        (None, True, None),
        (None, False, b"Name: other\nVersion: 0.1.0\n\n"),
    ],
)
def test_project_wheel_strict_inventory_metadata_source(extra, corrupt, metadata, tmp_path):
    _, _, release = source_archive(tmp_path)
    with pytest.raises(runtime.RuntimeErrorSafe):
        runtime._project_wheel(
            project_wheel(release, extra=extra, corrupt_source=corrupt, metadata=metadata), release
        )


def test_process_output_and_total_deadline_have_no_raw_reflection():
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime._run(["/usr/bin/python3", "-I", "-c", "print('PRIVATE-' * 4096)"], 5, 10)
    assert caught.value.reason == "process_output_bound" and "PRIVATE" not in str(caught.value)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime._run(["/usr/bin/python3", "-I", "-c", "import time; time.sleep(5)"], 0.1, 10)
    assert caught.value.reason == "process_deadline"


def test_asset_progress_is_content_free_stderr_only(monkeypatch, capsys):
    import itertools

    clock = itertools.count(0, 5)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    raw = runtime._run(
        ["/usr/bin/python3", "-I", "-c", "import time; time.sleep(.03); print('PRIVATE-BODY')"],
        300,
        1024,
        progress_phase="uv_asset_download",
    )
    assert raw == b"PRIVATE-BODY\n"
    captured = capsys.readouterr()
    assert captured.out == "" and "PRIVATE" not in captured.err
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert records
    for record in records:
        assert set(record) == {"phase", "received_pipe_bytes", "elapsed_seconds"}
        assert record["phase"] == "uv_asset_download"
        assert 0 <= record["received_pipe_bytes"] <= len(raw)
        assert record["elapsed_seconds"] >= 10
    assert all(
        b["elapsed_seconds"] - a["elapsed_seconds"] >= 10 for a, b in itertools.pairwise(records)
    )


def test_other_children_do_not_emit_progress(capsys):
    assert (
        runtime._run(["/usr/bin/python3", "-I", "-c", "print('fixture')"], 5, 1024) == b"fixture\n"
    )
    assert capsys.readouterr().err == ""


def test_unapproved_progress_phase_denied_before_child(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime._run(["unused"], 5, 1024, progress_phase="PRIVATE")
    assert caught.value.reason == "progress_phase"


def test_build_isolated_offline_pinned_tools_no_pip_or_source_build(tmp_path, monkeypatch):
    source_archive_path, source_hash, release = source_archive(tmp_path)
    source = tmp_path / "source"
    runtime._release_tool().extract_archive(source_archive_path.read_bytes(), source_hash, source)
    calls = []
    monkeypatch.setattr(runtime, "_download", lambda a: b"fixture-tool")
    raw = project_wheel(release)

    def run(command, *args, **kwargs):
        calls.append((command, kwargs))
        if "from hatchling.build import build_wheel" in " ".join(command):
            (Path(command[-1]) / f"api_quota_broker-{VERSION}-py3-none-any.whl").write_bytes(raw)
        return b""

    monkeypatch.setattr(runtime, "_run", run)
    assert runtime._build(source, release, tmp_path / "uv", tmp_path) == raw
    assert len(calls) == 3
    for command, _ in calls[:2]:
        assert (
            "--offline" in command
            and "--no-config" in command
            and "--no-python-downloads" in command
        )
    install = calls[1][0]
    assert "--require-hashes" in install and "--no-index" in install and "--no-build" not in install
    assert install[install.index("--only-binary") + 1] == ":all:"
    assert calls[2][0][1] == "-I" and calls[2][1]["cwd"] == source
    assert "pip" not in calls[2][0]


def cache_fixture(tmp_path, monkeypatch, prepared):
    bundle, _, _, _, _ = prepared
    cache = tmp_path / "artifacts"
    cache.mkdir()
    uv_raw = (bundle / "tools" / runtime.UV_NAME).read_bytes()
    (cache / runtime.UV_NAME).write_bytes(uv_raw)
    for artifact in runtime.RUNTIME:
        (cache / artifact.name).write_bytes((bundle / "wheelhouse" / artifact.name).read_bytes())
    bodies = {a.name: ("fixture-" + a.name).encode() for a in runtime.BUILD_TOOLS}
    monkeypatch.setattr(
        runtime,
        "BUILD_TOOLS",
        tuple(
            a._replace(sha256=digest(bodies[a.name]), size=len(bodies[a.name]))
            for a in runtime.BUILD_TOOLS
        ),
    )
    for name, raw in bodies.items():
        (cache / name).write_bytes(raw)
    return cache


def test_complete_cache_prepare_never_downloads(tmp_path, monkeypatch, prepared):
    cache = cache_fixture(tmp_path, monkeypatch, prepared)
    _, _, source, source_hash, _ = prepared
    monkeypatch.setattr(runtime, "_download", prohibited)
    monkeypatch.setattr(runtime, "_download_uv", prohibited)
    receipt = runtime.prepare_runtime(
        source, source_hash, tmp_path / "offline-output", artifacts_dir=cache
    )
    assert receipt["status"] == "verified"
    assert len(runtime._artifact_cache(cache)) == 10
    assert receipt["preparation"] == runtime.PREPARATION


@pytest.mark.parametrize(
    "mode", ["missing", "extra", "directory", "symlink", "fifo", "hardlink", "corrupt", "oversized"]
)
def test_cache_preflight_rejects_all_bad_inputs_before_output_or_child(
    mode, tmp_path, monkeypatch, prepared
):
    cache = cache_fixture(tmp_path, monkeypatch, prepared)
    _, _, source, source_hash, _ = prepared
    target = cache / runtime.BUILD_TOOLS[0].name
    if mode == "missing":
        target.unlink()
    elif mode == "extra":
        (cache / "unknown").write_bytes(b"fixture")
    elif mode == "directory":
        (cache / "unknown-directory").mkdir()
    elif mode == "symlink":
        target.unlink()
        target.symlink_to(cache / runtime.RUNTIME[0].name)
    elif mode == "fifo":
        target.unlink()
        os.mkfifo(target)
    elif mode == "hardlink":
        os.link(target, tmp_path / "linked-artifact")
    elif mode == "corrupt":
        original = target.read_bytes()
        target.write_bytes(b"X" + original[1:])
    else:
        target.write_bytes(target.read_bytes() + b"X")
    monkeypatch.setattr(runtime, "_download", prohibited)
    monkeypatch.setattr(runtime, "_download_uv", prohibited)
    monkeypatch.setattr(runtime, "_run", prohibited)
    output = tmp_path / "never-created"
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime.prepare_runtime(source, source_hash, output, artifacts_dir=cache)
    assert caught.value.phase == "artifacts_preflight"
    assert not output.exists()


@pytest.mark.parametrize(
    "index,phase", [(1, "build_venv"), (2, "install_build_tools"), (3, "project_wheel")]
)
def test_actual_build_child_failure_keeps_stage(index, phase, tmp_path, monkeypatch):
    archive, source_hash, release = source_archive(tmp_path)
    source = tmp_path / "source"
    runtime._release_tool().extract_archive(archive.read_bytes(), source_hash, source)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if len(calls) == index:
            raise runtime.RuntimeErrorSafe("process_failed")
        return b""

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime, "_download", prohibited)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime._build(
            source,
            release,
            tmp_path / "uv",
            tmp_path,
            artifacts={a.name: b"fixture" for a in runtime.BUILD_TOOLS},
        )
    assert caught.value.phase == phase and caught.value.reason == "process_failed"
    assert len(calls) == index


def test_cache_cli_identifies_zero_network_without_changing_receipt(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        runtime,
        "prepare_runtime",
        lambda *args, **kwargs: calls.append(kwargs) or {"status": "verified"},
    )
    assert (
        runtime.main(
            [
                "prepare",
                "--archive",
                "source",
                "--manifest-sha256",
                "hash",
                "--output",
                "output",
                "--artifacts-dir",
                "cache",
            ]
        )
        == 0
    )
    assert calls == [{"artifacts_dir": Path("cache")}]
    assert json.loads(capsys.readouterr().out) == {
        "status": "verified",
        "acquisition": "offline_cache",
        "network_calls": 0,
    }


def test_target_smoke_runs_local_crypto_pdf_without_network_or_credentials(monkeypatch):
    import importlib.metadata
    import platform
    import socket
    import sysconfig

    versions = {a.package: a.version for a in runtime.RUNTIME} | {"api-quota-broker": VERSION}
    monkeypatch.setattr(platform, "python_version", lambda: "3.14.4")
    monkeypatch.setattr(platform, "libc_ver", lambda: ("glibc", "2.43"))
    monkeypatch.setattr(sysconfig, "get_config_var", lambda key: 0)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: versions[name])

    class NoSocket(socket.socket):
        def __new__(cls, *args, **kwargs):
            return prohibited(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", NoSocket)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(os, "getenv", prohibited)
    result = runtime.target_smoke()
    assert result["status"] == "passed"
    assert result["checks"] == [
        "dependency_versions",
        "project_cli_import",
        "cffi",
        "fernet",
        "aesgcm",
        "pdf",
        "ssl_ca",
        "sqlite",
        "curl_present",
    ]


@pytest.mark.parametrize("version,gil", [("3.12.3", False), ("3.14.4", True)])
def test_target_smoke_wrong_python_or_gil_denied(version, gil, monkeypatch):
    import platform
    import sysconfig

    monkeypatch.setattr(platform, "python_version", lambda: version)
    monkeypatch.setattr(sysconfig, "get_config_var", lambda key: gil)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime.target_smoke()
    assert caught.value.reason == "target_python"


def test_target_dependency_failure_stops_before_application_import(monkeypatch):
    import importlib.metadata
    import platform
    import sysconfig

    monkeypatch.setattr(platform, "python_version", lambda: "3.14.4")
    monkeypatch.setattr(platform, "libc_ver", lambda: ("glibc", "2.43"))
    monkeypatch.setattr(sysconfig, "get_config_var", lambda key: 0)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "wrong")
    monkeypatch.setattr(importlib, "import_module", prohibited)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime.target_smoke()
    assert caught.value.reason == "target_dependency_version"


def test_application_import_failure_cli_returns_safe_fixed_error(monkeypatch, capsys):
    import importlib.metadata
    import platform
    import sysconfig

    versions = {a.package: a.version for a in runtime.RUNTIME} | {"api-quota-broker": VERSION}
    monkeypatch.setattr(platform, "python_version", lambda: "3.14.4")
    monkeypatch.setattr(platform, "libc_ver", lambda: ("glibc", "2.43"))
    monkeypatch.setattr(sysconfig, "get_config_var", lambda key: 0)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: versions[name])
    calls = []

    def fail(name):
        calls.append(name)
        raise ImportError("PRIVATE-RAW-MESSAGE")

    monkeypatch.setattr(importlib, "import_module", fail)
    assert runtime.main(["--target-smoke"]) == 2
    output = capsys.readouterr()
    assert calls == ["quota_broker.cli"]
    assert "PRIVATE" not in output.out + output.err
    assert json.loads(output.out)["reason"] == "invalid_input"


def test_wrong_tool_version_preserves_partial_output_no_retry(prepared, tmp_path, monkeypatch):
    _, _, source, source_hash, _ = prepared
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: b"uv 0.9.6\n")
    monkeypatch.setattr(runtime, "_download", prohibited)
    output = tmp_path / "wrong-version"
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime.prepare_runtime(source, source_hash, output)
    assert caught.value.reason == "tool_version"
    assert (output / "tools/uv").exists() and not (output / "runtime-manifest.json").exists()


def test_build_host_pin_rejects_target_prepare_before_read_or_network(monkeypatch):
    monkeypatch.setattr(runtime.sys, "version_info", (3, 14, 4))
    monkeypatch.setattr(runtime, "_read", prohibited)
    monkeypatch.setattr(runtime, "_download_uv", prohibited)
    with pytest.raises(runtime.RuntimeErrorSafe) as caught:
        runtime.prepare_runtime(Path("never-read"), "a" * 64, Path("never-created"))
    assert caught.value.reason == "build_host_python"


@pytest.mark.parametrize(
    "argv", [["--url", "PRIVATE"], ["prepare"], ["verify"], ["--target-smoke", "plan"]]
)
def test_cli_errors_do_not_reflect_input(argv, capsys):
    assert runtime.main(argv) == 2
    result = capsys.readouterr()
    assert "PRIVATE" not in result.out + result.err
    assert json.loads(result.out)["reason"] == "options"
