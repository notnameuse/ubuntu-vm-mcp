import hashlib
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest

from tests.conftest import FakeSFTP, RemoteFile, make_config, make_local_entry, make_plan, make_remote_entry
from tools.sync.models import LocalFileEntry, RemoteFileEntry, SyncPlanItem
from tools.sync.plan import build_sync_plan, compute_confirm_digest
from tools.sync.remote_index import RemoteIndex


def test_classifies_same_size_content_change_as_overwrite():
    local = [make_local_entry("src/main.cpp", b"int b;\n")]
    remote = RemoteIndex(files={"src/main.cpp": make_remote_entry(b"int a;\n", "src/main.cpp")})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.items["src/main.cpp"].kind == "overwrite"
    assert plan.items["src/main.cpp"].remote_sha256 == hashlib.sha256(b"int a;\n").hexdigest()


def test_window_defers_remaining_sorted_files():
    local = [make_local_entry(f"src/{name}.cpp", name.encode()) for name in ("c", "a", "b")]

    plan = build_sync_plan(local, RemoteIndex(), make_config(max_files=2))

    assert [item.relative_path for item in plan.window] == ["src/a.cpp", "src/b.cpp"]
    assert [item.relative_path for item in plan.deferred] == ["src/c.cpp"]
    assert plan.has_more is True
    assert plan.summary["deferred"] == 1


def test_case_only_rename_blocks_instead_of_creating_duplicate():
    local = [make_local_entry("main.cpp", b"x")]
    remote = RemoteIndex(files={"Main.cpp": make_remote_entry(b"x", "Main.cpp")})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "blocked_conflict"
    assert plan.window == []


def test_remote_index_errors_fail_plan_with_empty_window():
    local = [make_local_entry("src/main.cpp", b"x")]
    remote = RemoteIndex(files={}, errors=[{"relative_path": "src", "error": "listdir failed"}])

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "failed"
    assert plan.window == []
    assert plan.summary["failed"] == 1


def test_aborted_remote_index_fails_plan():
    plan = build_sync_plan([make_local_entry("a.cpp", b"x")], RemoteIndex(aborted=True), make_config())

    assert plan.status == "failed"
    assert plan.window == []


def test_same_size_unchecked_hash_fails_plan():
    local = [make_local_entry("main.cpp", b"x")]
    remote_entry = replace(make_remote_entry(b"x", "main.cpp"), hash_checked=False)
    remote = RemoteIndex(files={"main.cpp": remote_entry})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "failed"
    assert plan.window == []
    assert plan.items["main.cpp"].kind == "failed"


def test_same_size_missing_hash_fails_plan():
    local = [make_local_entry("main.cpp", b"x")]
    remote_entry = RemoteFileEntry("main.cpp", 1, None, 0o644, hash_checked=True)
    remote = RemoteIndex(files={"main.cpp": remote_entry})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "failed"
    assert plan.window == []


def test_size_difference_without_hash_is_normal_overwrite():
    local = [make_local_entry("main.cpp", b"new")]
    remote_entry = RemoteFileEntry("main.cpp", 4, None, 0o640, hash_checked=False)
    remote = RemoteIndex(files={"main.cpp": remote_entry})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "ready"
    assert plan.items["main.cpp"].kind == "overwrite"
    assert plan.items["main.cpp"].final_mode == 0o640
    assert plan.items["main.cpp"].mode_source == "remote_preserved"


def test_unchanged_and_remote_only_are_reported():
    local = [make_local_entry("same.cpp", b"same")]
    remote = RemoteIndex(
        files={
            "same.cpp": make_remote_entry(b"same", "same.cpp"),
            "remote.cpp": make_remote_entry(b"remote", "remote.cpp"),
        }
    )

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "no_changes"
    assert plan.items["same.cpp"].kind == "unchanged"
    assert plan.items["remote.cpp"].kind == "remote_only"
    assert plan.items["remote.cpp"].local is None
    assert plan.window == []
    assert plan.summary["remote_only"] == 1


def test_shell_script_mode_is_always_executable():
    local = [make_local_entry("startup.sh", b"echo ok\r\n")]
    remote = RemoteIndex(files={"startup.sh": make_remote_entry(b"old", "startup.sh", 0o640)})

    plan = build_sync_plan(local, remote, make_config())

    item = plan.items["startup.sh"]
    assert item.kind == "overwrite"
    assert item.final_mode == 0o755
    assert item.mode_source == "shell_default"


def test_new_regular_file_uses_default_mode():
    plan = build_sync_plan([make_local_entry("main.cpp", b"x")], RemoteIndex(), make_config())

    item = plan.items["main.cpp"]
    assert item.kind == "add"
    assert item.final_mode == 0o644
    assert item.mode_source == "new_default"


def test_confirm_digest_binds_root_and_window():
    local = [make_local_entry("main.cpp", b"x")]
    first = build_sync_plan(local, RemoteIndex(root_realpath="/home/liu/project"), make_config())
    second = build_sync_plan(local, RemoteIndex(root_realpath="/home/liu/other"), make_config())

    assert first.confirm_digest != second.confirm_digest
    assert first.confirm_digest == compute_confirm_digest(first.root_realpath, first.window)


def test_unicode_normalization_collision_blocks():
    composed = unicodedata.normalize("NFC", "é.cpp")
    decomposed = unicodedata.normalize("NFD", "é.cpp")
    local = [make_local_entry(composed, b"x")]
    remote = RemoteIndex(files={decomposed: make_remote_entry(b"x", decomposed)})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "blocked_conflict"
    assert plan.window == []


def test_temp_file_is_not_classified():
    temp_name = ".vm_sync-" + "a" * 32 + ".tmp"
    remote = RemoteIndex(files={temp_name: make_remote_entry(b"temp", temp_name)})

    plan = build_sync_plan([], remote, make_config())

    assert temp_name not in plan.items
    assert plan.status == "no_changes"


def test_conflict_blocks_window_even_with_valid_candidates():
    local = [make_local_entry("main.cpp", b"x"), make_local_entry("other.cpp", b"y")]
    remote = RemoteIndex(
        files={"Main.cpp": make_remote_entry(b"x", "Main.cpp")},
        conflicts=[{"kind": "link", "path": "linked", "error": "link"}],
    )

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "blocked_conflict"
    assert plan.window == []
    assert plan.conflicts


def test_summary_counts_all_categories():
    local = [
        make_local_entry("add.cpp", b"a"),
        make_local_entry("over.cpp", b"new"),
        make_local_entry("same.cpp", b"same"),
        make_local_entry("defer.cpp", b"d"),
    ]
    remote = RemoteIndex(
        files={
            "over.cpp": make_remote_entry(b"old", "over.cpp"),
            "same.cpp": make_remote_entry(b"same", "same.cpp"),
            "remote.cpp": make_remote_entry(b"r", "remote.cpp"),
        }
    )

    plan = build_sync_plan(local, remote, make_config(max_files=2))

    assert plan.summary["add"] == 2
    assert plan.summary["overwrite"] == 1
    assert plan.summary["unchanged"] == 1
    assert plan.summary["remote_only"] == 1
    assert plan.summary["deferred"] == 1
    assert plan.summary["failed"] == 0


def test_failed_item_has_blocking_reason():
    local = [make_local_entry("main.cpp", b"x")]
    remote_entry = replace(make_remote_entry(b"x", "main.cpp"), hash_checked=False)

    plan = build_sync_plan(local, RemoteIndex(files={"main.cpp": remote_entry}), make_config())

    assert isinstance(plan.items["main.cpp"], SyncPlanItem)
    assert plan.items["main.cpp"].blocked_reason


def test_local_scan_errors_fail_plan_with_empty_window():
    local = [make_local_entry("main.cpp", b"x")]

    plan = build_sync_plan(
        local,
        RemoteIndex(),
        make_config(),
        local_errors=[{"relative_path": "secret.cpp", "error": "denied"}],
    )

    assert plan.status == "failed"
    assert plan.window == []
    assert plan.errors == [{"relative_path": "secret.cpp", "error": "denied"}]


def test_confirm_digest_binds_final_mode():
    local = [make_local_entry("main.cpp", b"new")]
    first = build_sync_plan(
        local,
        RemoteIndex(files={"main.cpp": RemoteFileEntry("main.cpp", 4, None, 0o644, False)}),
        make_config(),
    )
    second = build_sync_plan(
        local,
        RemoteIndex(files={"main.cpp": RemoteFileEntry("main.cpp", 4, None, 0o640, False)}),
        make_config(),
    )

    assert first.confirm_digest != second.confirm_digest


def test_confirm_digest_binds_raw_hash_for_shell_script(tmp_path):
    first_local = make_local_entry("startup.sh", b"echo ok\r\n", tmp_path=tmp_path / "first")
    second_local = make_local_entry("startup.sh", b"echo ok\n", tmp_path=tmp_path / "second")
    first = build_sync_plan([first_local], RemoteIndex(), make_config())
    second = build_sync_plan([second_local], RemoteIndex(), make_config())

    assert first_local.upload_sha256 == second_local.upload_sha256
    assert first_local.raw_sha256 != second_local.raw_sha256
    assert first.confirm_digest != second.confirm_digest


def test_shell_content_same_wrong_mode_is_overwrite():
    local = [make_local_entry("startup.sh", b"echo ok\n")]
    remote = RemoteIndex(files={"startup.sh": make_remote_entry(b"echo ok\n", "startup.sh", 0o640)})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.items["startup.sh"].kind == "overwrite"
    assert plan.items["startup.sh"].final_mode == 0o755


def test_make_plan_uses_production_classification():
    local = make_local_entry("same.cpp", b"same")
    remote = FakeSFTP({"same.cpp": RemoteFile(b"same", 0o640)})

    helper_plan = make_plan(local, remote=remote)
    production_plan = build_sync_plan(
        [local],
        RemoteIndex(
            root_realpath=remote.root,
            files={"same.cpp": make_remote_entry(b"same", "same.cpp", 0o640)},
        ),
        make_config(),
    )

    assert helper_plan.items == production_plan.items
    assert helper_plan.summary == production_plan.summary
    assert helper_plan.confirm_digest == production_plan.confirm_digest


def test_confirm_digest_rejects_remote_only_item():
    item = SyncPlanItem(relative_path="remote.cpp", kind="remote_only", local=None)

    with pytest.raises(ValueError, match="local files"):
        compute_confirm_digest("/home/liu/project", [item])


def test_blocked_summary_is_informational_and_window_is_empty():
    local = [make_local_entry("main.cpp", b"x"), make_local_entry("new.cpp", b"new")]
    remote = RemoteIndex(files={"Main.cpp": make_remote_entry(b"x", "Main.cpp")})

    plan = build_sync_plan(local, remote, make_config())

    assert plan.status == "blocked_conflict"
    assert plan.summary["add"] == 2
    assert plan.window == []
    assert plan.has_more is False


def test_failed_summary_counts_only_affected_local_paths():
    local = [make_local_entry("src/a.cpp", b"a"), make_local_entry("src/b.cpp", b"b")]

    plan = build_sync_plan(
        local,
        RemoteIndex(),
        make_config(),
        local_errors=[{"relative_path": "src/a.cpp", "error": "denied"}],
    )

    assert plan.summary["failed"] == 1
    assert "src/a.cpp" in plan.items
    assert "src/b.cpp" not in plan.items


def test_shell_add_forces_executable_even_for_non_scanner_entry():
    local = LocalFileEntry(
        relative_path="x.sh",
        absolute_path=Path("x.sh"),
        raw_size=1,
        raw_sha256="a" * 64,
        upload_size=1,
        upload_sha256="b" * 64,
        line_ending_converted=False,
        final_mode=0o644,
        mode_source="new_default",
    )

    plan = build_sync_plan([local], RemoteIndex(), make_config())

    assert plan.items["x.sh"].final_mode == 0o755
    assert plan.items["x.sh"].mode_source == "shell_default"
