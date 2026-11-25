import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from tests.conftest import FakeSFTP, RemoteFile, make_config, make_local_entry, make_plan, sha256
from tools.sync.scanner import prepare_upload_bytes, scan_local


def test_shell_script_normalizes_crlf_in_memory(tmp_path: Path):
    script = tmp_path / "startup.sh"
    script.write_bytes(b"#!/bin/sh\r\necho ok\r\n")

    prepared = prepare_upload_bytes(script, True)

    assert prepared.data == b"#!/bin/sh\necho ok\n"
    assert prepared.size == len(prepared.data)
    assert prepared.sha256 == hashlib.sha256(prepared.data).hexdigest()
    assert prepared.line_ending_converted is True
    assert script.read_bytes() == b"#!/bin/sh\r\necho ok\r\n"


def test_non_script_keeps_bytes(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_bytes(b"int x;\r\n")

    prepared = prepare_upload_bytes(source, False)

    assert prepared.data == b"int x;\r\n"
    assert prepared.line_ending_converted is False
    assert source.read_bytes() == b"int x;\r\n"


def test_scan_skips_symlink_and_empty_directory(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "empty").mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.cpp").write_bytes(b"int main(){}\n")
    (project / "build").mkdir()
    (project / "build" / "old.o").write_bytes(b"binary")
    try:
        (project / "link.cpp").symlink_to(project / "src" / "main.cpp")
    except NotImplementedError:
        pytest.skip("file symlinks are unavailable on this platform")
    except OSError as exc:
        if os.name != "nt":
            raise
        pytest.skip(f"file symlinks are unavailable for this user: {exc}")
    else:
        assert (project / "link.cpp").is_symlink()

    entries, errors = scan_local(make_config(tmp_path))

    assert [entry.relative_path for entry in entries] == ["src/main.cpp"]
    assert errors == []


def test_scan_prunes_nested_excluded_directories(tmp_path: Path):
    project = tmp_path / "project"
    (project / "src" / "build").mkdir(parents=True)
    (project / "src" / "build" / "old.o").write_bytes(b"binary")
    (project / "src" / "main.cpp").write_bytes(b"int main(){}\n")
    (project / "docs" / "superpowers").mkdir(parents=True)
    (project / "docs" / "superpowers" / "plan.md").write_bytes(b"plan")

    entries, errors = scan_local(make_config(tmp_path))

    assert [entry.relative_path for entry in entries] == ["src/main.cpp"]
    assert errors == []


def test_scan_records_read_errors(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.cpp"
    source.write_bytes(b"int main(){}\n")
    original_open = Path.open

    def failing_open(path, *args, **kwargs):
        if path == source:
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    entries, errors = scan_local(make_config(tmp_path))

    assert entries == []
    assert errors == [{"relative_path": "main.cpp", "error": "denied"}]


def test_scan_unicode_and_space_names(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "src 目录").mkdir()
    (project / "src 目录" / "配置 file.cpp").write_bytes(b"data")

    entries, errors = scan_local(make_config(tmp_path))

    assert [entry.relative_path for entry in entries] == ["src 目录/配置 file.cpp"]
    assert errors == []


def test_make_local_entry_produces_raw_and_upload_hashes(tmp_path: Path):
    entry = make_local_entry("startup.sh", b"echo ok\r\n", tmp_path=tmp_path)

    assert entry.raw_size == len(b"echo ok\r\n")
    assert entry.upload_size == len(b"echo ok\n")
    assert entry.raw_sha256 == sha256(b"echo ok\r\n")
    assert entry.upload_sha256 == sha256(b"echo ok\n")
    assert entry.final_mode == 0o755
    assert entry.line_ending_converted is True


def test_scan_timeout_is_reported(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.cpp").write_bytes(b"data")
    config = replace(make_config(tmp_path), sync_scan_timeout_seconds=0)
    monkeypatch.setattr("tools.sync.scanner.time.monotonic", lambda: 1.0)

    entries, errors = scan_local(config)

    assert entries == []
    assert errors
    assert "timed out" in errors[0]["error"]


def test_scan_regular_file_defaults(tmp_path: Path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "main.cpp").write_bytes(b"data")

    entries, errors = scan_local(make_config(tmp_path))

    assert errors == []
    assert entries[0].final_mode == 0o644
    assert entries[0].mode_source == "new_default"


def test_shell_script_without_crlf_is_not_marked_converted(tmp_path: Path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "startup.sh").write_bytes(b"echo ok\n")

    entries, errors = scan_local(make_config(tmp_path))

    assert errors == []
    assert entries[0].line_ending_converted is False


def test_fake_sftp_directory_permissions():
    remote = FakeSFTP()
    remote.mkdir("/home/liu/project/new", mode=0o700)
    assert remote.lstat("/home/liu/project/new").st_mode & 0o777 == 0o700

    remote.chmod("/home/liu/project/new", 0o755)

    assert remote.lstat("/home/liu/project/new").st_mode & 0o777 == 0o755


def test_fake_sftp_rejects_absolute_path_outside_root():
    remote = FakeSFTP()
    with pytest.raises(ValueError, match="escapes"):
        remote.lstat("/home/liu/project-other/main.cpp")


def test_make_plan_digest_binds_root_and_window(tmp_path: Path):
    plan = make_plan("main.cpp", b"new", tmp_path=tmp_path)
    payload = {
        "root_realpath": plan.root_realpath,
        "window": [
            {
                "path": item.relative_path,
                "raw_sha256": item.local.raw_sha256,
                "upload_sha256": item.local.upload_sha256,
                "remote_sha256": item.remote_sha256,
                "mode": item.final_mode,
            }
            for item in plan.window
        ],
    }
    expected = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    assert plan.confirm_digest == expected


def test_make_config_state_dir_is_inside_package(tmp_path: Path):
    config = make_config(tmp_path)

    assert config.state_dir == config.package_root / "sync_state"
    assert config.state_dir.is_dir()


def test_fake_sftp_rejects_parent_traversal():
    remote = FakeSFTP()
    with pytest.raises(ValueError, match="parent traversal"):
        remote.lstat("src/../../outside")


def test_fake_sftp_rejects_write_handles_and_duplicate_mkdir():
    remote = FakeSFTP()
    with pytest.raises(NotImplementedError):
        remote.open("main.cpp", "wb")
    remote.mkdir("new")
    with pytest.raises(FileExistsError):
        remote.mkdir("new")


def test_fake_sftp_mkdir_is_single_level():
    remote = FakeSFTP()
    with pytest.raises(FileNotFoundError):
        remote.mkdir("src/nested")
    remote.mkdir("src")
    remote.mkdir("src/nested")
    assert remote.lstat("src/nested").st_mode & 0o777 == 0o777


def test_fake_sftp_channel_records_timeout():
    remote = FakeSFTP()
    remote.get_channel().settimeout(42)
    assert remote.get_channel().timeouts == [42]


def test_fake_sftp_rejects_windows_absolute_path():
    remote = FakeSFTP()
    with pytest.raises(ValueError, match="escapes"):
        remote.lstat("D:/outside/main.cpp")


def test_fake_sftp_overwrite_preserves_mode():
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    remote.put("main.cpp", b"new")
    assert remote.mode("main.cpp") == 0o640
