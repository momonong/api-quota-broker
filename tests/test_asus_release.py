"""Release build/verify/extract fixtures; no remote, credential or Git writes."""

import copy
import importlib.util
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "scripts" / "build_asus_release.py"
SPEC = importlib.util.spec_from_file_location("build_asus_release", MODULE)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
HEAD = "a" * 40


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    for path in release._paths():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture payload: " + path + "\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "api-quota-broker"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    provenance = {
        "head": HEAD,
        "working_tree_dirty": True,
        "payload_dirty": True,
        "included_changes": [{"path": "src/quota_broker/families.py", "kind": "untracked"}],
        "description": release.DESCRIPTION,
    }
    (root / "deploy/asus/gateway.disabled.json").write_text('{"targets":[]}\n', encoding="utf-8")
    monkeypatch.setattr(release, "read_provenance", lambda *args: copy.deepcopy(provenance))

    def forbidden(*args, **kwargs):
        raise AssertionError("process/auth/network execution attempted")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return root


def build(source):
    return release.build_release(source)


def unpack(artifact):
    members = {}
    with tarfile.open(fileobj=io.BytesIO(artifact.archive), mode="r:") as bundle:
        for member in bundle:
            stream = bundle.extractfile(member)
            assert stream is not None
            members[member.name] = stream.read()
    return members


def mutate_tar(artifact, *, omit=None, append=None, modify=None):
    output = io.BytesIO()
    with (
        tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as target,
        tarfile.open(fileobj=io.BytesIO(artifact.archive), mode="r:") as original,
    ):
        for member in original:
            if member.name == omit:
                continue
            stream = original.extractfile(member)
            assert stream is not None
            data = stream.read()
            if modify is not None:
                data = modify(member, data)
            member.size = len(data)
            target.addfile(member, io.BytesIO(data) if member.isreg() else None)
        if append is not None:
            member, data = append
            target.addfile(member, io.BytesIO(data) if member.isreg() else None)
    return output.getvalue()


def check(artifact, raw):
    return release.verify_archive(raw, artifact.manifest["payload_manifest_sha256"])


def test_fixed_allowlist_captures_all_dirty_code_and_excludes_private_state(source, monkeypatch):
    private = "fixture-private-secret-content-not-for-release"
    excluded = (
        ".state/old.db",
        "secrets/key",
        ".git/config",
        ".venv/bin/python",
        "tests/private.py",
        "data/catalog/provider.json",
        "deploy/asus/extra-secret.conf",
    )
    for path in excluded:
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(private, encoding="utf-8")
    added = source / "src/quota_broker/families.py"
    added.write_text("# new unpublished modality\n", encoding="utf-8")
    reads = []
    original = release._read_regular

    def read(root, path, bound=release.MAX_FILE_BYTES):
        assert path in release._paths()
        reads.append(path)
        return original(root, path, bound)

    monkeypatch.setattr(release, "_read_regular", read)
    artifact = build(source)
    assert set(reads) == set(release._paths())
    members = unpack(artifact)
    assert set(members) == set(release._paths()) | {release.MANIFEST_MEMBER}
    assert private.encode() not in artifact.archive
    assert members["src/quota_broker/families.py"] == b"# new unpublished modality\n"
    assert artifact.manifest["release"]["source"]["head"] == HEAD
    assert artifact.manifest["release"]["source"]["payload_dirty"] is True
    assert (
        artifact.manifest["artifact_id"]
        == "release-" + artifact.manifest["payload_manifest_sha256"]
    )
    assert (
        release.verify_manifest(artifact.archive, release._canonical(artifact.manifest))
        == artifact.manifest
    )


def test_deterministic_archive_ignores_local_permissions_timestamps_and_owners(source):
    first = build(source)
    for index, path in enumerate(release._paths()):
        target = source / path
        target.chmod(0o600)
        os.utime(target, (1_000_000 + index, 2_000_000 + index))
    second = build(source)
    assert first == second
    assert release._digest(first.archive) == first.manifest["archive_sha256"]
    for record in first.manifest["release"]["files"]:
        assert record["sha256"] == release._digest((source / record["path"]).read_bytes())


def test_dirty_payload_changes_hash_without_claiming_new_head(source):
    first = build(source)
    (source / "src/quota_broker/families.py").write_text(
        "# changed unpublished bytes\n", encoding="utf-8"
    )
    second = build(source)
    assert (
        first.manifest["release"]["source"]["head"] == second.manifest["release"]["source"]["head"]
    )
    assert first.manifest["artifact_id"] != second.manifest["artifact_id"]
    assert first.manifest["archive_sha256"] != second.manifest["archive_sha256"]


def test_disabled_gateway_is_explicit_required_payload_with_normalized_mode(source):
    path = "deploy/asus/gateway.disabled.json"
    assert path in release.DEPLOY_FILES
    artifact = build(source)
    assert unpack(artifact)[path] == b'{"targets":[]}\n'
    record = next(row for row in artifact.manifest["release"]["files"] if row["path"] == path)
    assert record["mode"] == 0o644
    (source / path).unlink()
    with pytest.raises(release.ReleaseError):
        build(source)


def test_swap_guard_is_explicit_required_payload_with_normalized_mode(source):
    path = "deploy/asus/swap_guard.py"
    assert path in release.DEPLOY_FILES
    artifact = build(source)
    assert unpack(artifact)[path] == ("# fixture payload: " + path + "\n").encode()
    record = next(row for row in artifact.manifest["release"]["files"] if row["path"] == path)
    assert record["mode"] == 0o644
    (source / path).unlink()
    with pytest.raises(release.ReleaseError):
        build(source)


@pytest.mark.parametrize(
    "path",
    [
        "deploy/asus/import_credentials.py",
        "deploy/asus/initial_install.py",
        "deploy/asus/acceptance.py",
        "deploy/asus/api-quota-broker-client.service",
        "deploy/asus/export_client_credential.py",
    ],
)
def test_r4_install_helpers_are_explicit_required_regular_644_payload(source, path):
    assert path in release.DEPLOY_FILES
    assert "deploy/asus/operator.py" not in release._paths()
    artifact = build(source)
    assert len(release._paths()) == len(artifact.manifest["release"]["files"]) == 44
    assert unpack(artifact)[path] == ("# fixture payload: " + path + "\n").encode()
    record = next(row for row in artifact.manifest["release"]["files"] if row["path"] == path)
    assert record["mode"] == 0o644
    (source / path).unlink()
    with pytest.raises(release.ReleaseError):
        build(source)


@pytest.mark.parametrize(
    "operation", ["unknown_py", "missing_py", "unknown_subpackage", "missing_deploy"]
)
def test_unknown_or_missing_required_payload_is_not_silently_omitted(source, operation):
    if operation == "unknown_py":
        (source / "src/quota_broker/new_provider.py").write_text("# not reviewed\n")
    elif operation == "missing_py":
        (source / release.SOURCE_FILES[0]).unlink()
    elif operation == "unknown_subpackage":
        (source / "src/quota_broker/new_package").mkdir()
    else:
        (source / release.DEPLOY_FILES[0]).unlink()
    with pytest.raises(release.ReleaseError):
        build(source)


def test_source_only_fixture_must_be_explicit_and_cannot_verify_as_production(source):
    for path in release.DEPLOY_FILES:
        (source / path).unlink()
    with pytest.raises(release.ReleaseError):
        build(source)
    artifact = release.build_release(source, source_only_fixture=True)
    assert artifact.manifest["release"]["policy"] == "source-only-fixture-v1"
    assert (
        release.verify_archive(
            artifact.archive, artifact.manifest["payload_manifest_sha256"], source_only_fixture=True
        )
        == artifact.manifest
    )
    with pytest.raises(release.ReleaseError):
        check(artifact, artifact.archive)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "parent_symlink"])
def test_source_symlink_nonregular_alias_and_parent_symlink_are_rejected(source, kind):
    target = source / "uv.lock"
    target.unlink()
    outside = source.parent / "external-private"
    outside.write_text("fixture-private-content")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_text("fixture-lock")
        package = source / "src/quota_broker"
        package.rename(source / "real_package")
        package.symlink_to(source / "real_package", target_is_directory=True)
    with pytest.raises(release.ReleaseError):
        build(source)
    assert outside.read_text() == "fixture-private-content"


def test_source_size_bound_and_source_change_fail_before_archive_output(source, monkeypatch):
    with pytest.raises(release.ReleaseError) as failure:
        release._read_regular(source, "pyproject.toml", bound=1)
    assert failure.value.reason == "source_size"
    monkeypatch.setattr(release, "MAX_PAYLOAD_BYTES", 1)
    with pytest.raises(release.ReleaseError) as failure:
        build(source)
    assert failure.value.reason == "source_size"


def test_source_modified_between_capture_passes_is_rejected(source, monkeypatch):
    original = release._read_regular
    count = 0

    def changed(root, path, bound=release.MAX_FILE_BYTES):
        nonlocal count
        count += 1
        if count == len(release._paths()) + 1:
            (source / path).write_text("# changed while building\n")
        return original(root, path, bound)

    monkeypatch.setattr(release, "_read_regular", changed)
    with pytest.raises(release.ReleaseError, match="release validation failed"):
        build(source)


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/absolute",
        ".state/secret.db",
        "src/quota_broker/not_reviewed.py",
        "deploy/asus/unreviewed.py",
        "deploy/asus/operator.py",
    ],
)
def test_unknown_traversal_or_absolute_archive_member_is_rejected(source, name):
    artifact = build(source)
    member = tarfile.TarInfo(name)
    raw = mutate_tar(artifact, append=(member, b""))
    with pytest.raises(release.ReleaseError):
        check(artifact, raw)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE]
)
def test_archive_member_type_cannot_write_through_links_or_nonregulars(source, kind):
    artifact = build(source)

    def modify(member, data):
        if member.name == "uv.lock":
            member.type = kind
            member.linkname = "../outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            return b""
        return data

    raw = mutate_tar(artifact, modify=modify)
    with pytest.raises(release.ReleaseError):
        check(artifact, raw)


def test_missing_duplicate_tampered_and_trailing_archive_are_rejected(source):
    artifact = build(source)
    duplicate = tarfile.TarInfo("uv.lock")
    for raw in (
        mutate_tar(artifact, omit="uv.lock"),
        mutate_tar(artifact, append=(duplicate, b"")),
        mutate_tar(
            artifact,
            modify=lambda member, data: data + b"changed" if member.name == "uv.lock" else data,
        ),
        artifact.archive + b"hidden bytes after tar EOF",
        artifact.archive[:-8192],
    ):
        with pytest.raises(release.ReleaseError):
            check(artifact, raw)


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "missing",
        "boolean_size",
        "upper_hash",
        "duplicate",
        "traversal",
        "dirty",
        "head",
        "version",
    ],
)
def test_manifest_schema_is_strict_even_when_mutated_hash_is_recomputed(source, change):
    artifact = build(source)
    members = unpack(artifact)
    manifest = copy.deepcopy(artifact.manifest["release"])
    if change == "extra":
        manifest["raw_secret"] = "untrusted message"
    elif change == "missing":
        manifest["files"].pop()
    elif change == "boolean_size":
        manifest["files"][0]["size"] = True
    elif change == "upper_hash":
        manifest["files"][0]["sha256"] = "A" * 64
    elif change == "duplicate":
        manifest["files"][1] = manifest["files"][0]
    elif change == "traversal":
        manifest["files"][0]["path"] = "../private"
    elif change == "dirty":
        manifest["source"]["working_tree_dirty"] = False
    elif change == "head":
        manifest["source"]["head"] = "untrusted message"
    else:
        manifest["package_version"] = "private message"
    encoded = release._canonical(manifest)
    payload = {path: data for path, data in members.items() if path != release.MANIFEST_MEMBER}
    raw = release._tar(payload, encoded)
    with pytest.raises(release.ReleaseError):
        release.verify_archive(raw, release._digest(encoded))


@pytest.mark.parametrize(
    "raw", [b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}', b"{" * 1500, b"[]"]
)
def test_duplicate_nonfinite_deep_and_wrong_manifest_json_is_rejected(source, raw):
    artifact = build(source)
    payload = {
        path: data for path, data in unpack(artifact).items() if path != release.MANIFEST_MEMBER
    }
    archive = release._tar(payload, raw)
    with pytest.raises(release.ReleaseError):
        release.verify_archive(archive, release._digest(raw))


def test_external_manifest_hash_size_and_fields_are_verified(source):
    artifact = build(source)
    for field, replacement in (
        ("archive_sha256", "b" * 64),
        ("archive_bytes", True),
        ("artifact_id", "untrusted"),
        ("schema_version", True),
    ):
        manifest = copy.deepcopy(artifact.manifest)
        manifest[field] = replacement
        with pytest.raises(release.ReleaseError):
            release.verify_manifest(artifact.archive, release._canonical(manifest))
    manifest = copy.deepcopy(artifact.manifest)
    manifest["unreviewed"] = True
    with pytest.raises(release.ReleaseError):
        release.verify_manifest(artifact.archive, release._canonical(manifest))
    with pytest.raises(release.ReleaseError):
        release.verify_archive(artifact.archive, "b" * 64)


def test_extract_verifies_before_write_and_never_overwrites(source, tmp_path):
    artifact = build(source)
    destination = tmp_path / "staging"
    with pytest.raises(release.ReleaseError):
        release.extract_archive(artifact.archive, "b" * 64, destination)
    assert not destination.exists()
    verified = release.extract_archive(
        artifact.archive, artifact.manifest["payload_manifest_sha256"], destination
    )
    assert verified == artifact.manifest
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for path, data in unpack(artifact).items():
        assert (destination / path).read_bytes() == data
        assert stat.S_IMODE((destination / path).stat().st_mode) == release._mode(path)
    (destination / "keep").write_text("preserve existing")
    with pytest.raises(FileExistsError):
        release.extract_archive(
            artifact.archive, artifact.manifest["payload_manifest_sha256"], destination
        )
    assert (destination / "keep").read_text() == "preserve existing"


def test_extract_rejects_symlink_destination_and_symlink_parent_without_writes(source, tmp_path):
    artifact = build(source)
    external = tmp_path / "external"
    external.mkdir()
    destination = tmp_path / "staging"
    destination.symlink_to(external, target_is_directory=True)
    with pytest.raises(FileExistsError):
        release.extract_archive(
            artifact.archive, artifact.manifest["payload_manifest_sha256"], destination
        )
    parent_link = tmp_path / "parent_link"
    parent_link.symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        release.extract_archive(
            artifact.archive, artifact.manifest["payload_manifest_sha256"], parent_link / "child"
        )
    assert tuple(external.iterdir()) == ()


def test_cli_dry_run_write_verify_and_exclusive_extract(source, tmp_path, capsys):
    assert release.main(["build", "--repo", str(source)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry_run" and report["allowlist"] == list(release._paths())
    destination = tmp_path / "artifacts"
    assert not destination.exists()
    assert release.main(["build", "--repo", str(source), "--output-dir", str(destination)]) == 0
    built = json.loads(capsys.readouterr().out)
    archive = destination / built["archive_name"]
    manifest_file = destination / built["manifest_name"]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_file.stat().st_mode) == 0o600
    trusted_hash = built["manifest"]["payload_manifest_sha256"]
    assert release.main(["verify", str(archive), trusted_hash]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "verified"
    assert (
        release.main(
            ["verify", str(archive), trusted_hash, "--extract", str(tmp_path / "extracted")]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["mode"] == "extracted"
    before = archive.read_bytes()
    assert release.main(["build", "--repo", str(source), "--output-dir", str(destination)]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "output_exists"
    assert archive.read_bytes() == before


def test_cli_invalid_arguments_and_manifest_never_echo_untrusted_paths(source, capsys):
    with pytest.raises(SystemExit):
        release.main(["build", "--secret", "private-sensitive-value"])
    assert "private-sensitive-value" not in capsys.readouterr().out
    assert release.main(["verify", str(source / "missing-private-path"), "bad-hash"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert set(error) == {"error", "reason"}
    assert "missing-private-path" not in json.dumps(error)
