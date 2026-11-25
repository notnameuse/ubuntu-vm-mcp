import stat
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import paramiko
import pytest

from tests.conftest import FakeSFTP, FakeSSH, RemoteFile, make_config, make_local_entry, sha256
from tools.sync.remote_index import assert_safe_remote_path, build_remote_index


def test_same_size_file_is_hashed_and_detects_change(tmp_path: Path):
    local = make_local_entry("src/main.cpp", b"int a;\n")
    remote = FakeSFTP({"src/main.cpp": RemoteFile(b"int b;\n", 0o644)})

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [local], time.monotonic() + 10)

    assert index.files["src/main.cpp"].sha256 == sha256(b"int b;\n")
    assert index.root_realpath == "/home/user/project"
    assert index.files["src/main.cpp"].hash_checked is True
    assert index.files["src/main.cpp"].size == len(b"int b;\n")


def test_different_size_local_target_is_still_hashed(tmp_path: Path):
    local = make_local_entry("src/main.cpp", b"new")
    remote = FakeSFTP({"src/main.cpp": RemoteFile(b"old-content", 0o640)})

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [local], time.monotonic() + 10)

    assert index.files["src/main.cpp"].sha256 == sha256(b"old-content")
    assert index.files["src/main.cpp"].hash_checked is True


def test_parent_symlink_is_conflict(tmp_path: Path):
    remote = FakeSFTP({"outside/file.cpp": RemoteFile(b"x", 0o644)})
    remote.symlinks.add("src")

    index = build_remote_index(
        FakeSSH(remote),
        make_config(tmp_path),
        [make_local_entry("src/file.cpp", b"x")],
        time.monotonic() + 10,
    )

    assert any(item["kind"] == "link" for item in index.conflicts)
    assert "src/file.cpp" not in index.files


def test_timeout_is_not_classified_as_unchanged(tmp_path: Path, monkeypatch):
    remote = FakeSFTP({"src/main.cpp": RemoteFile(b"int a;\n", 0o644)})
    now = [0.0]
    monkeypatch.setattr("tools.sync.remote_index.time.monotonic", lambda: now[0])
    remote.read_hook = lambda: now.__setitem__(0, 6.0)

    index = build_remote_index(
        FakeSSH(remote),
        make_config(tmp_path),
        [make_local_entry("src/main.cpp", b"int a;\n")],
        5.0,
    )

    assert index.errors
    assert index.files["src/main.cpp"].sha256 is None
    assert index.files["src/main.cpp"].hash_checked is False


def test_excluded_remote_directory_is_counted_and_not_walked(tmp_path: Path):
    remote = FakeSFTP({"build/old.o": RemoteFile(b"binary"), "src/main.cpp": RemoteFile(b"x")})

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert index.remote_excluded_count == 1
    assert "build" not in index.directories
    assert "build/old.o" not in index.files
    assert "src/main.cpp" in index.files


def test_remote_only_file_is_reported_without_hash(tmp_path: Path):
    remote = FakeSFTP({"remote-only.cpp": RemoteFile(b"remote")})

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert index.files["remote-only.cpp"].size == len(b"remote")
    assert index.files["remote-only.cpp"].sha256 is None


def test_target_directory_is_type_conflict(tmp_path: Path):
    remote = FakeSFTP({"src/main.cpp/child.txt": RemoteFile(b"child")})

    index = build_remote_index(
        FakeSSH(remote),
        make_config(tmp_path),
        [make_local_entry("src/main.cpp", b"file")],
        time.monotonic() + 10,
    )

    assert any(item["kind"] == "type_conflict" and item["path"] == "src/main.cpp" for item in index.conflicts)


def test_parent_regular_file_is_type_conflict(tmp_path: Path):
    remote = FakeSFTP({"src": RemoteFile(b"not a directory")})

    index = build_remote_index(
        FakeSSH(remote),
        make_config(tmp_path),
        [make_local_entry("src/main.cpp", b"file")],
        time.monotonic() + 10,
    )

    assert any(item["kind"] == "type_conflict" and item["path"] == "src" for item in index.conflicts)


def test_temp_files_are_ignored(tmp_path: Path):
    remote = FakeSFTP(
        {
            ".vm_sync-" + "a" * 32 + ".tmp": RemoteFile(b"temp"),
            ".vm_sync-nothex.tmp": RemoteFile(b"business"),
        }
    )

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert ".vm_sync-" + "a" * 32 + ".tmp" not in index.files
    assert ".vm_sync-nothex.tmp" in index.files


def test_unicode_space_newline_and_backslash_names(tmp_path: Path):
    names = ["目录/配置 file.cpp", "line\nbreak.cpp", "back\\slash.cpp"]
    remote = FakeSFTP({name: RemoteFile(b"x") for name in names})

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert set(names).issubset(index.files)
    assert index.errors == []


def test_assert_safe_remote_path_rejects_unsafe_relative_paths(tmp_path: Path):
    remote = FakeSFTP()
    root = remote.normalize("/home/user/project")

    for path in ("", "/etc/passwd", "../secret", "src/../../secret"):
        with pytest.raises(ValueError):
            assert_safe_remote_path(remote, root, path)


def test_assert_safe_remote_path_checks_parents_and_leaf_type(tmp_path: Path):
    remote = FakeSFTP({"src/main.cpp": RemoteFile(b"x"), "dir/child": RemoteFile(b"x")})
    remote.symlinks.add("linked")
    root = remote.normalize("/home/user/project")

    assert assert_safe_remote_path(remote, root, "src/main.cpp") == "/home/user/project/src/main.cpp"
    assert assert_safe_remote_path(remote, root, "new/file.cpp") == "/home/user/project/new/file.cpp"
    with pytest.raises(ValueError, match="link"):
        assert_safe_remote_path(remote, root, "linked/file.cpp")
    with pytest.raises(ValueError, match="regular"):
        assert_safe_remote_path(remote, root, "dir")


def test_root_symlink_is_rejected(tmp_path: Path):
    remote = FakeSFTP()
    remote.symlinks.add("")

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert index.errors
    assert index.aborted is True
    assert index.files == {}


def test_remote_mode_is_recorded(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"x", 0o640)})

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert index.files["main.cpp"].mode == 0o640
    assert stat.S_ISREG(remote.lstat("main.cpp").st_mode)


def test_timeout_stops_remaining_enumeration(tmp_path: Path, monkeypatch):
    remote = FakeSFTP({"a.cpp": RemoteFile(b"a"), "b.cpp": RemoteFile(b"b")})
    now = [0.0]
    monkeypatch.setattr("tools.sync.remote_index.time.monotonic", lambda: now[0])
    remote.read_hook = lambda: now.__setitem__(0, 6.0)
    local = [make_local_entry("a.cpp", b"a"), make_local_entry("b.cpp", b"b")]

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), local, 5.0)

    assert "a.cpp" in index.files
    assert "b.cpp" not in index.files
    assert len([item for item in index.errors if "timed out" in item["error"]]) == 1


def test_special_remote_file_is_type_conflict(tmp_path: Path):
    remote = FakeSFTP()
    remote.specials.add("pipe")

    index = build_remote_index(
        FakeSSH(remote),
        make_config(tmp_path),
        [make_local_entry("pipe", b"x")],
        time.monotonic() + 10,
    )

    assert any(item["kind"] == "type_conflict" and item["path"] == "pipe" for item in index.conflicts)


def test_assert_safe_remote_path_rejects_missing_leaf_when_required(tmp_path: Path):
    remote = FakeSFTP()
    root = remote.normalize("/home/user/project")

    with pytest.raises(FileNotFoundError):
        assert_safe_remote_path(remote, root, "missing.cpp", allow_missing_leaf=False)


def test_missing_remote_root_is_structured_error(tmp_path: Path):
    config = replace(make_config(tmp_path), project_root="/home/user/missing")
    index = build_remote_index(FakeSSH(FakeSFTP()), config, [], time.monotonic() + 10)

    assert index.errors
    assert index.files == {}


def test_excluded_remote_file_is_counted(tmp_path: Path):
    remote = FakeSFTP({"src/main.o": RemoteFile(b"binary")})

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert index.remote_excluded_count == 1
    assert "src/main.o" not in index.files


def test_leaf_symlink_is_type_conflict(tmp_path: Path):
    remote = FakeSFTP()
    remote.symlinks.add("main.cpp")

    index = build_remote_index(
        FakeSSH(remote),
        make_config(tmp_path),
        [make_local_entry("main.cpp", b"x")],
        time.monotonic() + 10,
    )

    assert any(item["kind"] == "type_conflict" and item["path"] == "main.cpp" for item in index.conflicts)


def test_top_level_timeout_marks_index_aborted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.sync.remote_index.time.monotonic", lambda: 0.0)

    index = build_remote_index(FakeSSH(FakeSFTP()), make_config(tmp_path), [], 0.0)

    assert index.aborted is True
    assert index.files == {}
    assert len(index.errors) == 1


def test_non_timeout_read_failure_blocks_only_that_hash(tmp_path: Path):
    remote = FakeSFTP({"a.cpp": RemoteFile(b"a"), "b.cpp": RemoteFile(b"b")})
    calls = [0]

    def fail_first_read():
        calls[0] += 1
        if calls[0] == 1:
            raise EOFError("lost")

    remote.read_hook = fail_first_read
    local = [make_local_entry("a.cpp", b"a"), make_local_entry("b.cpp", b"b")]

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), local, time.monotonic() + 10)

    assert index.files["a.cpp"].hash_checked is False
    assert index.files["b.cpp"].hash_checked is True
    assert index.errors


def test_listdir_failure_is_structured_and_other_directory_survives(tmp_path: Path):
    remote = FakeSFTP({"src/a.cpp": RemoteFile(b"a"), "other/b.cpp": RemoteFile(b"b")})
    remote.listdir_fail.add("src")

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert "src/a.cpp" not in index.files
    assert "other/b.cpp" in index.files
    assert any(item["relative_path"] == "src" for item in index.errors)


def test_open_sftp_failure_is_structured(tmp_path: Path):
    ssh = MagicMock()
    ssh.open_sftp.side_effect = paramiko.SSHException("lost")

    index = build_remote_index(ssh, make_config(tmp_path), [], time.monotonic() + 10)

    assert index.aborted is True
    assert index.errors
    assert index.files == {}


def test_assert_safe_remote_path_rejects_leaf_link_and_parent_file(tmp_path: Path):
    remote = FakeSFTP({"src": RemoteFile(b"file")})
    remote.symlinks.add("main.cpp")
    root = remote.normalize("/home/user/project")

    with pytest.raises(ValueError, match="not a directory"):
        assert_safe_remote_path(remote, root, "src/main.cpp")
    with pytest.raises(ValueError, match="link"):
        assert_safe_remote_path(remote, root, "main.cpp")


@pytest.mark.parametrize("root", ["", "relative", "/"])
def test_assert_safe_remote_path_rejects_invalid_root(root: str):
    with pytest.raises(ValueError, match="root_realpath"):
        assert_safe_remote_path(FakeSFTP(), root, "main.cpp")


def test_dot_directory_entries_are_ignored(tmp_path: Path):
    remote = FakeSFTP({"src/main.cpp": RemoteFile(b"x")})
    remote.emit_dot_entries = True

    index = build_remote_index(FakeSSH(remote), make_config(tmp_path), [], time.monotonic() + 10)

    assert index.errors == []
    assert "src/main.cpp" in index.files
    assert "." not in index.directories
    assert ".." not in index.directories
