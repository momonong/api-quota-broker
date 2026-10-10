"""Prepare a reviewed source release's offline ASUS runtime on the build host.

Default invocation is a pure plan. Prepare never installs on ASUS, reads
credentials, builds dependencies from source, or follows HTTP redirects.
The single approved GitHub redirect is restricted to its exact asset host.
"""

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any, NamedTuple

UV_NAME = "uv-x86_64-unknown-linux-gnu.tar.gz"
UV_URL = "https://github.com/astral-sh/uv/releases/download/0.9.5/" + UV_NAME
UV_HASH = "2cf10babba653310606f8b49876cfb679928669e7ddaa1fb41fb00ce73e64f66"
UV_SIZE = 21_370_871
UV_TOTAL_SECONDS = 300
WHEEL_TOTAL_SECONDS = 20
FILE_BOUND = 64 * 1024 * 1024
MANIFEST_BOUND = 256 * 1024
HASH = re.compile(r"[a-f0-9]{64}")
TARGET = {
    "python": "3.14.4",
    "implementation": "CPython",
    "architecture": "x86_64",
    "gil_disabled": False,
    "minimum_glibc": "2.17",
}
PREPARATION = {
    "artifact_count": 10,
    "max_http_requests": 11,
    "max_download_wall_seconds": 480,
    "build_python": "3.12.3",
    "uv_version": "0.9.5",
    "source_date_epoch": 1580601600,
    "target_install": False,
    "credential_reads": 0,
    "provider_calls": 0,
}


class RuntimeErrorSafe(ValueError):
    def __init__(self, reason: str, *, phase: str = "asus_runtime"):
        super().__init__("runtime preparation failed")
        self.reason = reason
        self.phase = phase


class Artifact(NamedTuple):
    package: str
    version: str
    name: str
    url: str
    sha256: str
    size: int


def _wheel(package: str, version: str, tag: str, path: str, digest: str, size: int) -> Artifact:
    name = package.replace("-", "_") + "-" + version + "-" + tag + ".whl"
    return Artifact(
        package,
        version,
        name,
        "https://files.pythonhosted.org/packages/" + path + "/" + name,
        digest,
        size,
    )


RUNTIME = (
    _wheel(
        "cryptography",
        "50.0.2",
        "cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64",
        "21/69/64cef1f702bf6657e0cc186ed1a2891d50d29fb41586b254e1c07adea261",
        "630ebfea3bf689d075f82316324ff7433dc447fe6bc1bfc76524b74b4a9567d2",
        4719841,
    ),
    _wheel(
        "cffi",
        "2.1.1",
        "cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64",
        "e9/02/4e7d553a7ac4b4238b38b3c1b80d486e9d4436f8d2acbf87a0997fe3f402",
        "b0431303acaea1089ad4b3e9ce4e6518193def1118d4073ca848635ee4ea2e96",
        221525,
    ),
    _wheel(
        "pycparser",
        "3.0",
        "py3-none-any",
        "0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3",
        "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992",
        48172,
    ),
    _wheel(
        "pypdf",
        "6.19.0",
        "py3-none-any",
        "3c/2c/c43c03eaf630435f023f1dc61ec4a4a78951ad5530a62c71cc89bde307b7",
        "7e5d6e730e7dae87d560a2cee218b852f6498c8be61966f3cd02ead971e48d14",
        395480,
    ),
)
BUILD_TOOLS = (
    _wheel(
        "hatchling",
        "1.27.0",
        "py3-none-any",
        "08/e7/ae38d7a6dfba0533684e0b2136817d667588ae3ec984c1a4e5df5eb88482",
        "d3a2f3567c4f926ea39849cdf924c7e99e6686c9c8e288ae1037c8fa2a5d937b",
        75794,
    ),
    _wheel(
        "packaging",
        "26.3",
        "py3-none-any",
        "63/34/ba1c580383c9eada3711951fef0795c80b829a078d72188184bcab9dd527",
        "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
        129956,
    ),
    _wheel(
        "pathspec",
        "1.1.1",
        "py3-none-any",
        "f1/d9/7fb5aa316bc299258e68c73ba3bddbc499654a07f151cba08f6153988714",
        "a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189",
        57328,
    ),
    _wheel(
        "pluggy",
        "1.6.0",
        "py3-none-any",
        "54/20/4d324d65cc6d9205fabedc306948156824eb9f0ee1633355a8f7ec5c66bf",
        "e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746",
        20538,
    ),
    _wheel(
        "trove-classifiers",
        "2026.9.21.13",
        "py3-none-any",
        "30/81/0da8afb52a71d0a4f2bd3152357b1a441e393b286374802b9d3addab4ab5",
        "8b1ff4f9c191b1040b71c37f1e445ab99732911e3cd91de52838453a854d7a17",
        14232,
    ),
)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeErrorSafe("manifest_schema")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    def constant(_: str) -> None:
        raise RuntimeErrorSafe("manifest_schema")

    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=constant)


def _directory(path: Path) -> int:
    parts = path.absolute().parts
    if ".." in parts:
        raise RuntimeErrorSafe("path")
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(path: Path, bound: int = FILE_BOUND) -> bytes:
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > bound:
                raise RuntimeErrorSafe("file_type_or_size")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(bound + 1)
            if len(raw) > bound:
                raise RuntimeErrorSafe("file_type_or_size")
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _new(path: Path, raw: bytes, mode: int = 0o644) -> None:
    parent = _directory(path.parent)
    try:
        fd = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(parent)


def _new_dir(path: Path) -> None:
    parent = _directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent)
    finally:
        os.close(parent)


def _run(
    command: list[str],
    timeout: float,
    bound: int,
    *,
    cwd: Path | None = None,
    progress_phase: str | None = None,
) -> bytes:
    """Kill on total deadline; bounded stdout and no raw stderr reflection."""
    if progress_phase not in (None, "uv_asset_download"):
        raise RuntimeErrorSafe("progress_phase")
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "SOURCE_DATE_EPOCH": str(PREPARATION["source_date_epoch"]),
            "PYTHONHASHSEED": "0",
        },
    ) as process:
        assert process.stdout is not None
        started = time.monotonic()
        deadline = started + timeout
        next_progress = started + 10
        raw = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeErrorSafe("process_deadline")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            raw.extend(chunk)
                            if len(raw) > bound:
                                raise RuntimeErrorSafe("process_output_bound")
                    now = time.monotonic()
                    if progress_phase is not None and now >= next_progress:
                        print(
                            _canonical(
                                {
                                    "phase": "uv_asset_download",
                                    "received_pipe_bytes": len(raw),
                                    "elapsed_seconds": round(now - started, 3),
                                }
                            ).decode(),
                            file=sys.stderr,
                            flush=True,
                        )
                        next_progress = now + 10
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise RuntimeErrorSafe("process_deadline") from None
            if code != 0:
                if command[0] == "/usr/bin/curl" and code == 28:
                    raise RuntimeErrorSafe("process_deadline")
                raise RuntimeErrorSafe("process_failed")
            return bytes(raw)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _download(artifact: Artifact) -> bytes:
    if artifact not in RUNTIME + BUILD_TOOLS:
        raise RuntimeErrorSafe("artifact_not_listed")
    marker = b"\nquota-runtime-status:"
    raw = _run(
        [
            "/usr/bin/curl",
            "--disable",
            "--silent",
            "--fail",
            "--proto",
            "=https",
            "--connect-timeout",
            "5",
            "--max-time",
            "20",
            "--max-redirs",
            "0",
            "--write-out",
            marker.decode() + "%{http_code}",
            artifact.url,
        ],
        20,
        artifact.size + 1024,
    )
    body, found, status = raw.rpartition(marker)
    if not found or status != b"200":
        raise RuntimeErrorSafe("download_status")
    if len(body) != artifact.size or _digest(body) != artifact.sha256:
        raise RuntimeErrorSafe("artifact_hash")
    return body


def _artifact_cache(directory: Path) -> dict[str, bytes]:
    """Validate the complete fixed input set before any output or executable."""
    expected = {a.name: (a.size, a.sha256) for a in RUNTIME + BUILD_TOOLS}
    expected[UV_NAME] = (UV_SIZE, UV_HASH)
    try:
        fd = _directory(directory)
        try:
            if set(os.listdir(fd)) != set(expected):
                raise RuntimeErrorSafe("artifact_inventory")
        finally:
            os.close(fd)
        result = {}
        for name, (size, digest) in expected.items():
            raw = _read(directory / name, size)
            if len(raw) != size or _digest(raw) != digest:
                raise RuntimeErrorSafe("artifact_hash")
            result[name] = raw
        return result
    except RuntimeErrorSafe as exc:
        raise RuntimeErrorSafe(exc.reason, phase="artifacts_preflight") from None
    except OSError:
        raise RuntimeErrorSafe("artifact_input", phase="artifacts_preflight") from None


def _run_stage(phase: str, command: list[str], timeout: float, bound: int, **kwargs: Any) -> bytes:
    try:
        return _run(command, timeout, bound, **kwargs)
    except RuntimeErrorSafe as exc:
        raise RuntimeErrorSafe(exc.reason, phase=phase) from None
    except OSError:
        raise RuntimeErrorSafe("process_start_failed", phase=phase) from None


def _curl(
    url: str,
    timeout: float,
    bound: int,
    *,
    headers: bool = False,
    progress_phase: str | None = None,
) -> tuple[bytes, bytes]:
    marker = b"\nquota-runtime-status:"
    command = [
        "/usr/bin/curl",
        "--disable",
        "--silent",
        "--fail",
        "--proto",
        "=https",
        "--connect-timeout",
        "5",
        "--max-time",
        str(timeout),
        "--max-redirs",
        "0",
    ]
    if headers:
        command += ["--dump-header", "-"]
    command += ["--write-out", marker.decode() + "%{http_code}", url]
    raw = (
        _run(command, timeout, bound + 1024)
        if progress_phase is None
        else _run(command, timeout, bound + 1024, progress_phase=progress_phase)
    )
    body, found, status = raw.rpartition(marker)
    if not found:
        raise RuntimeErrorSafe("download_status")
    return body, status


def _download_uv() -> bytes:
    """One anonymous fixed GitHub GET, at most one exact approved asset redirect."""
    deadline = time.monotonic() + UV_TOTAL_SECONDS
    try:
        first, status = _curl(UV_URL, UV_TOTAL_SECONDS, UV_SIZE + 65536, headers=True)
    except RuntimeErrorSafe as exc:
        raise RuntimeErrorSafe(exc.reason, phase="uv_origin_download") from None
    head, separator, body = first.partition(b"\r\n\r\n")
    if not separator or not head.startswith(b"HTTP/"):
        raise RuntimeErrorSafe("tool_download_headers")
    if status == b"302":
        headers = BytesParser().parsebytes(head.split(b"\r\n", 1)[1] + b"\r\n\r\n")
        locations = headers.get_all("Location", [])
        if len(locations) != 1:
            raise RuntimeErrorSafe("tool_redirect")
        location = locations[0]
        parsed = urllib.parse.urlsplit(location)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "release-assets.githubusercontent.com"
            or parsed.netloc != "release-assets.githubusercontent.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith("/")
            or not location.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in location)
        ):
            raise RuntimeErrorSafe("tool_redirect")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeErrorSafe("process_deadline", phase="uv_asset_download")
        try:
            body, status = _curl(location, remaining, UV_SIZE, progress_phase="uv_asset_download")
        except RuntimeErrorSafe as exc:
            raise RuntimeErrorSafe(exc.reason, phase="uv_asset_download") from None
    if status != b"200":
        raise RuntimeErrorSafe("tool_download_status")
    if len(body) != UV_SIZE or _digest(body) != UV_HASH:
        raise RuntimeErrorSafe("tool_archive_hash")
    return body


def _uv_binary(raw: bytes) -> bytes:
    if len(raw) != UV_SIZE or _digest(raw) != UV_HASH:
        raise RuntimeErrorSafe("tool_archive_hash")
    prefix = UV_NAME.removesuffix(".tar.gz")
    allowed = {prefix, prefix + "/uv", prefix + "/uvx"}
    binary: bytes | None = None
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            name = member.name.removesuffix("/") if member.isdir() else member.name
            if name not in allowed or name in seen or member.size > FILE_BOUND:
                raise RuntimeErrorSafe("tool_archive_members")
            seen.add(name)
            if name == prefix:
                if not member.isdir():
                    raise RuntimeErrorSafe("tool_archive_members")
            else:
                if not member.isreg():
                    raise RuntimeErrorSafe("tool_archive_members")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeErrorSafe("tool_archive_members")
                data = stream.read(FILE_BOUND + 1)
                if len(data) != member.size:
                    raise RuntimeErrorSafe("tool_archive_members")
                if member.name.endswith("/uv"):
                    binary = data
    if binary is None or not binary.startswith(b"\x7fELF"):
        raise RuntimeErrorSafe("tool_archive_members")
    return binary


def _requirements(artifacts: tuple[Artifact, ...]) -> bytes:
    return "".join(
        f"{a.package}=={a.version} --hash=sha256:{a.sha256}\n" for a in artifacts
    ).encode()


def _release_tool() -> Any:
    path = Path(__file__).with_name("build_asus_release.py")
    spec = importlib.util.spec_from_file_location("asus_release_verifier", path)
    if spec is None or spec.loader is None:
        raise RuntimeErrorSafe("source_verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_source(source: Path, release: dict[str, Any]) -> None:
    import tomllib

    lock = tomllib.loads(_read(source / "uv.lock").decode())
    packages = {p["name"]: p for p in lock["package"]}
    for artifact in RUNTIME:
        package = packages.get(artifact.package, {})
        expected = {"url": artifact.url, "hash": "sha256:" + artifact.sha256, "size": artifact.size}
        if package.get("version") != artifact.version or not any(
            all(w.get(k) == v for k, v in expected.items()) for w in package.get("wheels", [])
        ):
            raise RuntimeErrorSafe("runtime_lock")
    project = tomllib.loads(_read(source / "pyproject.toml").decode())
    if (
        project["project"]["name"] != "api-quota-broker"
        or project["project"]["version"] != release["package_version"]
        or project["build-system"]
        != {"requires": ["hatchling>=1.25"], "build-backend": "hatchling.build"}
        or project["project"]["dependencies"] != ["cryptography>=44.0.0", "pypdf>=6.0.0"]
        or project["tool"]["hatch"]
        != {"build": {"targets": {"wheel": {"packages": ["src/quota_broker"]}}}}
    ):
        raise RuntimeErrorSafe("build_contract")
    for record in release["files"]:
        if _digest(_read(source / record["path"])) != record["sha256"]:
            raise RuntimeErrorSafe("source_changed")


def _project_wheel(raw: bytes, release: dict[str, Any]) -> str:
    version = release["package_version"]
    prefix = f"api_quota_broker-{version}.dist-info/"
    expected = {
        r["path"].removeprefix("src/"): r
        for r in release["files"]
        if r["path"].startswith("src/quota_broker/")
    }
    allowed = set(expected) | {
        prefix + name for name in ["METADATA", "WHEEL", "RECORD", "entry_points.txt"]
    }
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if (
            set(names) != allowed
            or len(names) != len(allowed)
            or sum(i.file_size for i in archive.infolist()) > FILE_BOUND
        ):
            raise RuntimeErrorSafe("project_wheel_members")
        contents = {}
        for member in archive.infolist():
            if member.file_size > FILE_BOUND or stat.S_ISLNK(member.external_attr >> 16):
                raise RuntimeErrorSafe("project_wheel_members")
            contents[member.filename] = archive.read(member)
        for name, record in expected.items():
            if len(contents[name]) != record["size"] or _digest(contents[name]) != record["sha256"]:
                raise RuntimeErrorSafe("project_wheel_source")
        metadata = BytesParser().parsebytes(contents[prefix + "METADATA"])
        if (
            metadata["Name"] != "api-quota-broker"
            or metadata["Version"] != version
            or metadata["Requires-Python"] != ">=3.12"
            or set(metadata.get_all("Requires-Dist", []))
            != {"cryptography>=44.0.0", "pypdf>=6.0.0"}
        ):
            raise RuntimeErrorSafe("project_wheel_metadata")
        wheel = BytesParser().parsebytes(contents[prefix + "WHEEL"])
        if wheel["Root-Is-Purelib"] != "true" or wheel.get_all("Tag") != ["py3-none-any"]:
            raise RuntimeErrorSafe("project_wheel_metadata")
        entries = contents[prefix + "entry_points.txt"].decode().strip()
        if entries != "[console_scripts]\nquota-broker = quota_broker.cli:main":
            raise RuntimeErrorSafe("project_wheel_metadata")
        records = list(csv.reader(io.StringIO(contents[prefix + "RECORD"].decode())))
        if len(records) != len(names) or {row[0] for row in records if len(row) == 3} != set(names):
            raise RuntimeErrorSafe("project_wheel_record")
        for name, digest, size in records:
            if name == prefix + "RECORD":
                if digest or size:
                    raise RuntimeErrorSafe("project_wheel_record")
            elif digest != "sha256=" + base64.urlsafe_b64encode(
                hashlib.sha256(contents[name]).digest()
            ).decode().rstrip("=") or size != str(len(contents[name])):
                raise RuntimeErrorSafe("project_wheel_record")
    return f"api_quota_broker-{version}-py3-none-any.whl"


def _build(
    source: Path,
    release: dict[str, Any],
    uv: Path,
    temporary: Path,
    *,
    artifacts: dict[str, bytes] | None = None,
) -> bytes:
    environment = temporary / "build-venv"
    _run_stage(
        "build_venv",
        [
            str(uv),
            "--offline",
            "--no-config",
            "--no-cache",
            "venv",
            "--python",
            sys.executable,
            "--no-python-downloads",
            "--no-managed-python",
            str(environment),
        ],
        30,
        8192,
    )
    tools = temporary / "build-tools"
    _new_dir(tools)
    for artifact in BUILD_TOOLS:
        try:
            _new(
                tools / artifact.name,
                _download(artifact) if artifacts is None else artifacts[artifact.name],
            )
        except RuntimeErrorSafe as exc:
            raise RuntimeErrorSafe(exc.reason, phase="download_build_tools") from None
    requirements = temporary / "build-tools.txt"
    _new(requirements, _requirements(BUILD_TOOLS))
    python = environment / "bin/python"
    _run_stage(
        "install_build_tools",
        [
            str(uv),
            "--offline",
            "--no-config",
            "--no-cache",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-python-downloads",
            "--no-managed-python",
            "--no-index",
            "--only-binary",
            ":all:",
            "--require-hashes",
            "--find-links",
            str(tools),
            "-r",
            str(requirements),
        ],
        30,
        8192,
    )
    destination = temporary / "project-dist"
    _new_dir(destination)
    _run_stage(
        "project_wheel",
        [
            str(python),
            "-I",
            "-c",
            "import sys; from hatchling.build import build_wheel; build_wheel(sys.argv[1])",
            str(destination),
        ],
        60,
        8192,
        cwd=source,
    )
    try:
        _check_source(source, release)
        files = list(destination.iterdir())
        if len(files) != 1:
            raise RuntimeErrorSafe("project_wheel_members")
        raw = _read(files[0])
        if files[0].name != _project_wheel(raw, release):
            raise RuntimeErrorSafe("project_wheel_name")
    except RuntimeErrorSafe as exc:
        raise RuntimeErrorSafe(exc.reason, phase="project_wheel") from None
    except OSError:
        raise RuntimeErrorSafe("project_wheel_input", phase="project_wheel") from None
    return raw


def build_plan() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "dry_run",
        "execution_authorized": False,
        "target": TARGET,
        "source": "trusted_hash_verified_release_archive",
        "downloads": [a._asdict() for a in RUNTIME + BUILD_TOOLS],
        "max_requests": 11,
        "artifact_count": 10,
        "max_artifacts": 10,
        "max_http_requests": 11,
        "wheel_per_artifact_total_seconds": WHEEL_TOTAL_SECONDS,
        "uv_total_seconds": UV_TOTAL_SECONDS,
        "max_download_wall_seconds": PREPARATION["max_download_wall_seconds"],
        "max_build_process_seconds": 120,
        "tool_probe_seconds": 5,
        "wheel_redirects": False,
        "retry_count": 0,
        "offline_artifacts": {
            "option": "--artifacts-dir",
            "acquisition": "offline_cache",
            "exact_names": sorted([UV_NAME] + [a.name for a in RUNTIME + BUILD_TOOLS]),
            "all_preflight_before_execution": True,
            "http_requests": 0,
            "fallback": False,
        },
        "tool_archive": {
            "name": UV_NAME,
            "url": UV_URL,
            "sha256": UV_HASH,
            "size": UV_SIZE,
            "redirect": {
                "initial_status": 302,
                "maximum_hops": 1,
                "scheme": "https",
                "host": "release-assets.githubusercontent.com",
                "authentication": False,
            },
        },
        "global_install": False,
        "target_install": False,
        "source_build_dependencies": False,
        "preparation": PREPARATION,
        "outputs": [
            "wheelhouse/",
            "tools/uv",
            "tools/" + UV_NAME,
            "runtime-target.txt",
            "requirements.txt",
            "source-manifest.json",
            "SHA256SUMS",
            "runtime-manifest.json",
        ],
    }


def prepare_runtime(
    archive: Path, source_hash: str, output: Path, *, artifacts_dir: Path | None = None
) -> dict[str, Any]:
    if sys.version_info[:3] != (3, 12, 3) or sys.platform != "linux":
        raise RuntimeErrorSafe("build_host_python")
    if not HASH.fullmatch(source_hash):
        raise RuntimeErrorSafe("source_manifest_hash")
    tool = _release_tool()
    raw_source = _read(archive, tool.MAX_ARCHIVE_BYTES)
    receipt = tool.verify_archive(raw_source, source_hash)
    with tempfile.TemporaryDirectory(prefix="quota-asus-build-") as temporary:
        workspace = Path(temporary)
        source = workspace / "source"
        tool.extract_archive(raw_source, source_hash, source)
        release = receipt["release"]
        _check_source(source, release)
        artifacts = None if artifacts_dir is None else _artifact_cache(artifacts_dir)
        # No download or executable is started until source/lock/new destination pass.
        _new_dir(output)
        _new_dir(output / "wheelhouse")
        _new_dir(output / "tools")
        tool_raw = _download_uv() if artifacts is None else artifacts[UV_NAME]
        binary = _uv_binary(tool_raw)
        _new(output / "tools" / UV_NAME, tool_raw)
        uv = output.absolute() / "tools/uv"
        _new(uv, binary, 0o755)
        if _run([str(uv), "--version"], 5, 4096).split()[:2] != [b"uv", b"0.9.5"]:
            raise RuntimeErrorSafe("tool_version")
        for artifact in RUNTIME:
            _new(
                output / "wheelhouse" / artifact.name,
                _download(artifact) if artifacts is None else artifacts[artifact.name],
            )
        project_raw = (
            _build(source, release, uv, workspace)
            if artifacts is None
            else _build(source, release, uv, workspace, artifacts=artifacts)
        )
        project_name = _project_wheel(project_raw, release)
        project_hash = _digest(project_raw)
        _new(output / "wheelhouse" / project_name, project_raw)
    _new(output / "runtime-target.txt", _requirements(RUNTIME))
    _new(
        output / "requirements.txt",
        _requirements(RUNTIME)
        + f"api-quota-broker=={release['package_version']} --hash=sha256:{project_hash}\n".encode(),
    )
    source_manifest = _canonical(release)
    if _digest(source_manifest) != source_hash:
        raise RuntimeErrorSafe("source_manifest_hash")
    _new(output / "source-manifest.json", source_manifest)
    paths = sorted(
        [
            "tools/uv",
            "tools/" + UV_NAME,
            "runtime-target.txt",
            "requirements.txt",
            "source-manifest.json",
        ]
        + ["wheelhouse/" + a.name for a in RUNTIME]
        + ["wheelhouse/" + project_name]
    )
    files = [
        {
            "path": p,
            "sha256": _digest(_read(output / p)),
            "size": len(_read(output / p)),
            "mode": 0o755 if p == "tools/uv" else 0o644,
        }
        for p in paths
    ]
    manifest = {
        "schema_version": 1,
        "policy": "asus-runtime-v1",
        "status": "prepared",
        "target": TARGET,
        "preparation": PREPARATION,
        "source": {
            "payload_manifest_sha256": source_hash,
            "archive_sha256": _digest(raw_source),
            "uv_lock_sha256": next(r["sha256"] for r in release["files"] if r["path"] == "uv.lock"),
            "head": release["source"]["head"],
            "package_version": release["package_version"],
        },
        "runtime": [a._asdict() for a in RUNTIME],
        "build_tools": [a._asdict() for a in BUILD_TOOLS],
        "tool_archive": {"name": UV_NAME, "url": UV_URL, "sha256": UV_HASH, "size": UV_SIZE},
        "project": {"name": project_name, "sha256": project_hash},
        "files": files,
    }
    manifest_raw = _canonical(manifest)
    _new(output / "runtime-manifest.json", manifest_raw)
    sums = (
        "".join(f"{r['sha256']}  {r['path']}\n" for r in files)
        + _digest(manifest_raw)
        + "  runtime-manifest.json\n"
    )
    _new(output / "SHA256SUMS", sums.encode())
    return verify_bundle(output, _digest(manifest_raw), source_hash)


def verify_bundle(
    bundle: Path, manifest_hash: str, source_hash: str | None = None
) -> dict[str, Any]:
    if not HASH.fullmatch(manifest_hash):
        raise RuntimeErrorSafe("runtime_manifest_hash")
    raw = _read(bundle / "runtime-manifest.json", MANIFEST_BOUND)
    if _digest(raw) != manifest_hash:
        raise RuntimeErrorSafe("runtime_manifest_hash")
    manifest = _json(raw)
    fields = {
        "schema_version",
        "policy",
        "status",
        "target",
        "preparation",
        "source",
        "runtime",
        "build_tools",
        "tool_archive",
        "project",
        "files",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != fields
        or manifest["schema_version"] != 1
        or type(manifest["schema_version"]) is not int
        or manifest["policy"] != "asus-runtime-v1"
        or manifest["status"] != "prepared"
        or _canonical(manifest["target"]) != _canonical(TARGET)
        or _canonical(manifest["preparation"]) != _canonical(PREPARATION)
        or manifest["runtime"] != [a._asdict() for a in RUNTIME]
        or manifest["build_tools"] != [a._asdict() for a in BUILD_TOOLS]
        or manifest["tool_archive"]
        != {"name": UV_NAME, "url": UV_URL, "sha256": UV_HASH, "size": UV_SIZE}
    ):
        raise RuntimeErrorSafe("manifest_schema")
    source = manifest["source"]
    if (
        not isinstance(source, dict)
        or set(source)
        != {
            "payload_manifest_sha256",
            "archive_sha256",
            "uv_lock_sha256",
            "head",
            "package_version",
        }
        or any(
            not isinstance(source[k], str) or not HASH.fullmatch(source[k])
            for k in ["payload_manifest_sha256", "archive_sha256", "uv_lock_sha256"]
        )
        or not isinstance(source["head"], str)
        or not re.fullmatch(r"[a-f0-9]{40}", source["head"])
        or source_hash is not None
        and source["payload_manifest_sha256"] != source_hash
    ):
        raise RuntimeErrorSafe("source_manifest_hash")
    source_raw = _read(bundle / "source-manifest.json", MANIFEST_BOUND)
    if _digest(source_raw) != source["payload_manifest_sha256"]:
        raise RuntimeErrorSafe("source_manifest_hash")
    release = _json(source_raw)
    _release_tool()._validate_manifest(source_raw, False)
    if (
        release["package_version"] != source["package_version"]
        or release["source"]["head"] != source["head"]
    ):
        raise RuntimeErrorSafe("source_manifest_hash")
    if (
        next(r["sha256"] for r in release["files"] if r["path"] == "uv.lock")
        != source["uv_lock_sha256"]
    ):
        raise RuntimeErrorSafe("runtime_lock")
    project = manifest["project"]
    if not isinstance(project, dict) or set(project) != {"name", "sha256"}:
        raise RuntimeErrorSafe("manifest_schema")
    # Validate the derived project name before using it as a path.
    if project[
        "name"
    ] != f"api_quota_broker-{source['package_version']}-py3-none-any.whl" or not re.fullmatch(
        r"api_quota_broker-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any.whl", project["name"]
    ):
        raise RuntimeErrorSafe("project_wheel_name")
    expected_paths = sorted(
        [
            "tools/uv",
            "tools/" + UV_NAME,
            "runtime-target.txt",
            "requirements.txt",
            "source-manifest.json",
        ]
        + ["wheelhouse/" + a.name for a in RUNTIME]
        + ["wheelhouse/" + project["name"]]
    )
    records = manifest["files"]
    if not isinstance(records, list) or len(records) != len(expected_paths):
        raise RuntimeErrorSafe("manifest_schema")
    contents = {}
    for expected, record in zip(expected_paths, records, strict=True):
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256", "size", "mode"}
            or record["path"] != expected
            or type(record["size"]) is not int
            or not 0 <= record["size"] <= FILE_BOUND
            or record["mode"] != (0o755 if expected == "tools/uv" else 0o644)
        ):
            raise RuntimeErrorSafe("manifest_schema")
        contents[expected] = _read(bundle / expected)
        if (
            len(contents[expected]) != record["size"]
            or _digest(contents[expected]) != record["sha256"]
            or stat.S_IMODE((bundle / expected).lstat().st_mode) != record["mode"]
        ):
            raise RuntimeErrorSafe("bundle_file_hash")
    if _uv_binary(contents["tools/" + UV_NAME]) != contents["tools/uv"]:
        raise RuntimeErrorSafe("tool_binary_hash")
    for a in RUNTIME:
        if (
            len(contents["wheelhouse/" + a.name]) != a.size
            or _digest(contents["wheelhouse/" + a.name]) != a.sha256
        ):
            raise RuntimeErrorSafe("artifact_hash")
    project_raw = contents["wheelhouse/" + project["name"]]
    if (
        _digest(project_raw) != project["sha256"]
        or _project_wheel(project_raw, release) != project["name"]
    ):
        raise RuntimeErrorSafe("project_wheel_hash")
    requirements = (
        _requirements(RUNTIME)
        + f"api-quota-broker=={source['package_version']} --hash=sha256:{project['sha256']}\n".encode()
    )
    if (
        contents["runtime-target.txt"] != _requirements(RUNTIME)
        or contents["requirements.txt"] != requirements
    ):
        raise RuntimeErrorSafe("requirements")
    expected_sums = (
        "".join(f"{r['sha256']}  {r['path']}\n" for r in records)
        + manifest_hash
        + "  runtime-manifest.json\n"
    )
    if _read(bundle / "SHA256SUMS", MANIFEST_BOUND) != expected_sums.encode():
        raise RuntimeErrorSafe("checksums")
    actual_paths = []
    for path in bundle.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise RuntimeErrorSafe("bundle_inventory")
        if stat.S_ISREG(info.st_mode):
            actual_paths.append(str(path.relative_to(bundle)))
        elif str(path.relative_to(bundle)) not in {"tools", "wheelhouse"}:
            raise RuntimeErrorSafe("bundle_inventory")
    actual_paths.sort()
    if actual_paths != sorted(expected_paths + ["runtime-manifest.json", "SHA256SUMS"]):
        raise RuntimeErrorSafe("bundle_inventory")
    return {
        "schema_version": 1,
        "status": "verified",
        "policy": "asus-runtime-v1",
        "runtime_manifest_sha256": manifest_hash,
        "source_payload_manifest_sha256": source["payload_manifest_sha256"],
        "project_wheel_sha256": project["sha256"],
        "target": TARGET,
        "preparation": PREPARATION,
        "files": records,
    }


def target_smoke() -> dict[str, Any]:
    """Only native local runtime checks; no account, key, service or network access."""
    import importlib.metadata
    import platform
    import sqlite3
    import ssl
    import struct
    import sysconfig

    if (
        platform.python_implementation() != "CPython"
        or platform.python_version() != "3.14.4"
        or platform.machine() != "x86_64"
        or struct.calcsize("P") != 8
        or sysconfig.get_config_var("Py_GIL_DISABLED")
    ):
        raise RuntimeErrorSafe("target_python")
    libc, version = platform.libc_ver()
    if libc != "glibc" or tuple(map(int, version.split(".")[:2])) < (2, 17):
        raise RuntimeErrorSafe("target_libc")
    for a in RUNTIME:
        if importlib.metadata.version(a.package) != a.version:
            raise RuntimeErrorSafe("target_dependency_version")
    from quota_broker import __version__ as project_source_version

    # Bind distribution metadata to the installed signed source, across releases.
    if importlib.metadata.version("api-quota-broker") != project_source_version:
        raise RuntimeErrorSafe("target_project_version")
    cli = importlib.import_module("quota_broker.cli")
    config = importlib.import_module("quota_broker.config")
    if not callable(cli.main) or not callable(config.load_gateway_config):
        raise RuntimeErrorSafe("target_project_import")
    importlib.import_module("_cffi_backend")
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from pypdf import PdfReader, PdfWriter

    cipher = Fernet(base64.urlsafe_b64encode(bytes(32)))
    if cipher.decrypt(cipher.encrypt(b"fixture")) != b"fixture":
        raise RuntimeErrorSafe("target_crypto")
    aead = AESGCM(bytes(32))
    if (
        aead.decrypt(bytes(12), aead.encrypt(bytes(12), b"fixture", b"fixture"), b"fixture")
        != b"fixture"
    ):
        raise RuntimeErrorSafe("target_crypto")
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    stream.seek(0)
    if len(PdfReader(stream).pages) != 1:
        raise RuntimeErrorSafe("target_pdf")
    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"] <= 0:
        raise RuntimeErrorSafe("target_ca_trust")
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE smoke(value INTEGER)")
        connection.execute("INSERT INTO smoke VALUES (1)")
        if connection.execute("SELECT value FROM smoke").fetchone() != (1,):
            raise RuntimeErrorSafe("target_sqlite")
    if not Path("/usr/bin/curl").is_file() or not os.access("/usr/bin/curl", os.X_OK):
        raise RuntimeErrorSafe("target_curl")
    return {
        "schema_version": 1,
        "status": "passed",
        "mode": "target_smoke",
        "target": TARGET,
        "checks": [
            "dependency_versions",
            "project_cli_import",
            "cffi",
            "fernet",
            "aesgcm",
            "pdf",
            "ssl_ca",
            "sqlite",
            "curl_present",
        ],
        "provider_calls": 0,
        "credential_reads": 0,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise RuntimeErrorSafe("options")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=True)
    parser.add_argument("--target-smoke", action="store_true")
    sub = parser.add_subparsers(dest="action", parser_class=_Parser)
    sub.add_parser("plan")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--archive", type=Path, required=True)
    prepare.add_argument("--manifest-sha256", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--artifacts-dir", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--manifest-sha256", required=True)
    verify.add_argument("--source-manifest-sha256", required=True)
    try:
        args = parser.parse_args(argv)
        if args.target_smoke:
            if args.action is not None:
                raise RuntimeErrorSafe("options")
            result = target_smoke()
        elif args.action in (None, "plan"):
            result = build_plan()
        elif args.action == "prepare":
            result = (
                prepare_runtime(args.archive, args.manifest_sha256, args.output)
                if args.artifacts_dir is None
                else prepare_runtime(
                    args.archive,
                    args.manifest_sha256,
                    args.output,
                    artifacts_dir=args.artifacts_dir,
                )
            )
            if args.artifacts_dir is not None:
                result = {**result, "acquisition": "offline_cache", "network_calls": 0}
        else:
            result = verify_bundle(args.bundle, args.manifest_sha256, args.source_manifest_sha256)
        print(_canonical(result).decode())
        return 0
    except (
        RuntimeErrorSafe,
        ImportError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        reason = exc.reason if isinstance(exc, RuntimeErrorSafe) else "invalid_input"
        print(
            _canonical(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "phase": exc.phase if isinstance(exc, RuntimeErrorSafe) else "asus_runtime",
                    "reason": reason,
                }
            ).decode()
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
