import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import paramiko

from tests.conftest import FakeSFTP, FakeSSH, RemoteFile, make_config, make_local_entry, make_plan
from tools.sync.transfer import apply_window


def test_rename_failure_keeps_original_and_stops(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    remote.fail_posix_rename = True

    result = apply_window(
        FakeSSH(remote),
        make_config(tmp_path),
        make_plan(remote=remote),
        time.monotonic() + 10,
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert remote.read("main.cpp") == b"old"
    assert remote.rename_calls == 1
    assert result["applied"] == []
    assert result["unprocessed"] == ["main.cpp"]


def test_preflight_failure_never_starts_writes(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    plan = make_plan("main.cpp", b"new", "later.cpp", b"later")
    plan.window[1].local.absolute_path.write_bytes(b"changed")

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["status"] == "rejected_stale"
    assert remote.writes == 0
    assert result["applied"] == []
    assert result["unprocessed"] == ["later.cpp", "main.cpp"]


def test_shell_script_gets_lf_content_and_executable_mode(tmp_path: Path):
    local = make_local_entry("startup.sh", b"#!/bin/sh\r\necho ok\r\n")
    remote = FakeSFTP()

    result = apply_window(FakeSSH(remote), make_config(tmp_path), make_plan(local), time.monotonic() + 10)

    assert result["success"] is True
    assert remote.read("startup.sh") == b"#!/bin/sh\necho ok\n"
    assert remote.mode("startup.sh") == 0o755
    assert local.absolute_path.read_bytes() == b"#!/bin/sh\r\necho ok\r\n"


def test_temp_hash_failure_keeps_original(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    remote.corrupt_temp_before_rename = True

    result = apply_window(
        FakeSSH(remote),
        make_config(tmp_path),
        make_plan(remote=remote),
        time.monotonic() + 10,
    )

    assert result["success"] is False
    assert remote.read("main.cpp") == b"old"
    assert remote.rename_calls == 0
    assert not any(Path(path).name.startswith(".vm_sync-") for path in remote.files)
    assert result["applied"] == []
    assert result["unprocessed"] == ["main.cpp"]


def test_post_upload_hash_mismatch_stops_batch(tmp_path: Path):
    remote = FakeSFTP()
    remote.corrupt_after_rename = True
    plan = make_plan("a.cpp", b"a", "b.cpp", b"b")

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["success"] is False
    assert result["applied"] == []
    assert result["unprocessed"] == ["a.cpp", "b.cpp"]


def test_partial_success_preserves_first_file(tmp_path: Path):
    remote = FakeSFTP()
    remote.fail_posix_rename_on_call = 2
    plan = make_plan("a.cpp", b"a", "b.cpp", b"b")

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["success"] is False
    assert result["status"] == "partial"
    assert result["applied"] == ["a.cpp"]
    assert result["unprocessed"] == ["b.cpp"]
    assert remote.read("a.cpp") == b"a"


def test_parent_directory_modes_are_set(tmp_path: Path):
    remote = FakeSFTP()
    plan = make_plan("new/dir/main.cpp", b"x")

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["success"] is True
    assert remote.lstat("new").st_mode & 0o777 == 0o755
    assert remote.lstat("new/dir").st_mode & 0o777 == 0o755
    assert remote.mode("new/dir/main.cpp") == 0o644


def test_only_exact_temp_files_are_cleaned(tmp_path: Path):
    temp_name = ".vm_sync-" + "a" * 32 + ".tmp"
    similar = ".vm_sync-nothex.tmp"
    remote = FakeSFTP({temp_name: RemoteFile(b"stale"), similar: RemoteFile(b"business")})

    result = apply_window(
        FakeSSH(remote),
        make_config(tmp_path),
        make_plan("main.cpp", b"new"),
        time.monotonic() + 10,
    )

    assert result["success"] is True
    assert temp_name not in remote.files
    assert similar in remote.files


def test_reusing_old_preview_is_rejected_after_apply(tmp_path: Path):
    remote = FakeSFTP()
    config = make_config(tmp_path)
    plan = make_plan("main.cpp", b"new")
    first = apply_window(FakeSSH(remote), config, plan, time.monotonic() + 10)
    second = apply_window(FakeSSH(remote), config, plan, time.monotonic() + 10)

    assert first["success"] is True
    assert second["status"] == "rejected_stale"
    assert second["applied"] == []
    assert remote.read("main.cpp") == b"new"


def test_progress_called_before_and_after_each_file(tmp_path: Path):
    calls = []
    plan = make_plan("a.cpp", b"a", "b.cpp", b"b")

    result = apply_window(
        FakeSSH(FakeSFTP()),
        make_config(tmp_path),
        plan,
        time.monotonic() + 10,
        progress=lambda: calls.append("tick"),
    )

    assert result["success"] is True
    assert len(calls) == 6


def test_overwrite_preserves_remote_mode(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})

    result = apply_window(
        FakeSSH(remote),
        make_config(tmp_path),
        make_plan(remote=remote),
        time.monotonic() + 10,
    )

    assert result["success"] is True
    assert remote.read("main.cpp") == b"new"
    assert remote.mode("main.cpp") == 0o640


def test_remote_change_between_preview_and_apply_is_rejected(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    plan = make_plan(remote=remote)
    remote.files["main.cpp"] = RemoteFile(b"old2", 0o640)

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["status"] == "rejected_stale"
    assert remote.writes == 0
    assert remote.read("main.cpp") == b"old2"


def test_target_becoming_symlink_returns_blocked_conflict(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    plan = make_plan(remote=remote)
    remote.files.pop("main.cpp")
    remote.symlinks.add("main.cpp")

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["status"] == "blocked_conflict"
    assert remote.writes == 0
    assert result["applied"] == []


def test_putfo_failure_cleans_temp_and_keeps_original(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    remote.fail_putfo = True

    result = apply_window(
        FakeSSH(remote),
        make_config(tmp_path),
        make_plan(remote=remote),
        time.monotonic() + 10,
    )

    assert result["success"] is False
    assert remote.read("main.cpp") == b"old"
    assert not any(Path(path).name.startswith(".vm_sync-") for path in remote.files)


def test_open_sftp_failure_returns_structured_failure(tmp_path: Path):
    ssh = MagicMock()
    ssh.open_sftp.side_effect = paramiko.SSHException("lost")
    plan = make_plan()

    result = apply_window(ssh, make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["status"] == "failed"
    assert result["applied"] == []
    assert result["unprocessed"] == ["main.cpp"]


def test_overwrite_without_preview_hash_is_rejected(tmp_path: Path):
    remote = FakeSFTP({"main.cpp": RemoteFile(b"old", 0o640)})
    plan = make_plan(remote=remote)
    plan.window[0] = replace(plan.window[0], remote_sha256=None)

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["status"] == "rejected_stale"
    assert remote.writes == 0
    assert remote.read("main.cpp") == b"old"


def test_parent_mkdir_race_to_symlink_is_blocked(tmp_path: Path):
    remote = FakeSFTP()
    remote.mkdir_race_path = "new"
    remote.mkdir_race_symlink = True
    plan = make_plan("new/main.cpp", b"new")

    result = apply_window(FakeSSH(remote), make_config(tmp_path), plan, time.monotonic() + 10)

    assert result["status"] == "blocked_conflict"
    assert result["applied"] == []
    assert remote.writes == 0
