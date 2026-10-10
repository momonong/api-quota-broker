"""Build public release/wheel inputs locally; never consult host evidence or data."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "tests/fixtures/asus-history"


def load(name):
    path = ROOT / "scripts/build_asus_release.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normal_review(directory):
    """Source-matched application archive and valid pure-Python wheel, in tmp only."""
    artifact = load("portable_release_builder").build_release(ROOT)
    with tarfile.open(fileobj=io.BytesIO(artifact.archive)) as archive:
        payload = {row.name: archive.extractfile(row).read() for row in archive}
    version = artifact.manifest["release"]["package_version"]
    dist = f"api_quota_broker-{version}.dist-info/"
    files = {
        name.removeprefix("src/"): raw
        for name, raw in payload.items()
        if name.startswith("src/quota_broker/")
    }
    files.update(
        {
            dist + "METADATA": (
                f"Metadata-Version: 2.1\nName: api-quota-broker\nVersion: {version}\n"
            ).encode(),
            dist + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            dist + "entry_points.txt": b"[console_scripts]\nquota-broker = quota_broker.cli:main\n",
        }
    )
    record = dist + "RECORD"
    records = io.StringIO()
    writer = csv.writer(records)
    for name, raw in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        writer.writerow((name, "sha256=" + digest, len(raw)))
    writer.writerow((record, "", ""))
    files[record] = records.getvalue().encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        for name, raw in sorted(files.items()):
            wheel.writestr(name, raw)
    (directory / "project.whl").write_bytes(buffer.getvalue())
    (directory / "source.tar").write_bytes(artifact.archive)
    (directory / "policy.json").write_text(
        json.dumps({"payload_manifest_sha256": artifact.manifest["payload_manifest_sha256"]})
    )
    return directory
