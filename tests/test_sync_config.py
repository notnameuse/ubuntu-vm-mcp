import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.sync.config import DEFAULT_EXCLUDES, config_fingerprint, is_excluded, load_sync_config


def write_config(
    tmp_path: Path,
    root: Path,
    package_root: Path,
    state_path: str = "sync_state",
    **overrides,
):
    path = tmp_path / "config.json"
    payload = {
        "ssh_host": "192.168.1.7",
        "ssh_port": 22,
        "ssh_user": "liu",
        "ssh_key_path": "key",
        "project_root": "/home/liu/project",
        "local_project_root": str(root),
        "sync_state_path": state_path,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_sync_config(path, package_root)


def test_rejects_filesystem_root(tmp_path: Path):
    (tmp_path / "package").mkdir()
    (tmp_path / "project").mkdir()
    with pytest.raises(ValueError, match="project root"):
        write_config(tmp_path, tmp_path.anchor, tmp_path / "package")


def test_rejects_state_outside_package(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    (tmp_path / "project").mkdir()
    with pytest.raises(ValueError, match="state"):
        write_config(tmp_path, tmp_path / "project", package, "../outside")


def test_rejects_remote_filesystem_root(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="project root"):
        write_config(tmp_path, project, package, project_root="/")


def test_rejects_missing_local_project(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    with pytest.raises(ValueError, match="project root"):
        write_config(tmp_path, tmp_path / "missing", package)


def test_default_excludes_are_always_present(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    config = write_config(tmp_path, project, package, sync_excludes=["secret.txt"])

    assert config.sync_excludes == ("secret.txt",)
    assert config.effective_excludes == DEFAULT_EXCLUDES + ("secret.txt",)
    assert is_excluded("src/secret.txt", config.effective_excludes)


def test_exclusions_match_each_directory_segment():
    assert is_excluded("src/build/a.cpp", DEFAULT_EXCLUDES)
    assert is_excluded("nested/.git/config", DEFAULT_EXCLUDES)
    assert is_excluded("cmake-build-debug/a.o", DEFAULT_EXCLUDES)
    assert is_excluded("src/main.o", DEFAULT_EXCLUDES)
    assert is_excluded("lib/libvision.so", DEFAULT_EXCLUDES)
    assert is_excluded("tools/program.exe", DEFAULT_EXCLUDES)
    assert is_excluded("docs/superpowers/spec.md", DEFAULT_EXCLUDES)
    assert not is_excluded("src/main.cpp", DEFAULT_EXCLUDES)
    assert not is_excluded("Build/a.cpp", DEFAULT_EXCLUDES)
    assert not is_excluded("src\\main.cpp", DEFAULT_EXCLUDES)
    assert not is_excluded("src/foo.git", DEFAULT_EXCLUDES)
    assert not is_excluded("docs/x.venv", DEFAULT_EXCLUDES)


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "src/../secret", ""])
def test_exclusions_reject_non_posix_or_unsafe_paths(path: str):
    with pytest.raises(ValueError):
        is_excluded(path, DEFAULT_EXCLUDES)


def test_config_fingerprint_is_stable_and_changes_with_sync_limit(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    first = write_config(tmp_path, project, package, sync_max_files_per_call=10)
    second = write_config(tmp_path, project, package, sync_max_files_per_call=10)
    changed = write_config(tmp_path, project, package, sync_max_files_per_call=11)

    assert config_fingerprint(first) == config_fingerprint(second)
    assert config_fingerprint(first) != config_fingerprint(changed)
    assert len(config_fingerprint(first)) == 64
    int(config_fingerprint(first), 16)


def test_state_symlink_escape_is_rejected(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = package / "state-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except NotImplementedError:
        pytest.skip("directory symlinks are unavailable on this platform")
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) not in (1312, 1314):
            raise
        pytest.skip("directory symlinks are unavailable for this user")

    with pytest.raises(ValueError, match="must stay inside"):
        write_config(tmp_path, project, package, "state-link")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ssh_host", "192.168.1.8"),
        ("ssh_port", 2222),
        ("ssh_user", "other"),
        ("ssh_key_path", "other-key"),
        ("verify_host_keys", False),
        ("project_root", "/home/liu/other"),
        ("sync_excludes", ["secret.txt"]),
        ("sync_max_files_per_call", 101),
    ],
)
def test_fingerprint_changes_for_each_security_field(tmp_path: Path, field: str, value):
    package = tmp_path / "package"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    baseline = write_config(tmp_path, project, package)
    changed = write_config(tmp_path, project, package, **{field: value})

    assert config_fingerprint(baseline) != config_fingerprint(changed)


def test_fingerprint_changes_with_local_project_root(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    other_project = tmp_path / "other-project"
    other_project.mkdir()

    baseline = write_config(tmp_path, project, package)
    changed = write_config(tmp_path, other_project, package)

    assert config_fingerprint(baseline) != config_fingerprint(changed)


def test_config_fingerprint_uses_canonical_json(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    config = write_config(tmp_path, project, package, sync_max_files_per_call=7)
    payload = {
        "ssh_host": config.ssh_host,
        "ssh_port": config.ssh_port,
        "ssh_user": config.ssh_user,
        "ssh_key_path": config.ssh_key_path,
        "verify_host_keys": config.verify_host_keys,
        "project_root": config.project_root,
        "local_project_root": str(config.local_project_root),
        "sync_excludes": list(config.sync_excludes),
        "sync_max_files_per_call": config.sync_max_files_per_call,
    }
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert config_fingerprint(config) == expected
