from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tools.sync.models import LocalFileEntry, RemoteFileEntry, SyncPlan, SyncPlanItem, SyncResult


def make_local_entry() -> LocalFileEntry:
    return LocalFileEntry(
        relative_path="src/main.cpp",
        absolute_path=Path("src/main.cpp"),
        raw_size=4,
        raw_sha256="a" * 64,
        upload_size=4,
        upload_sha256="a" * 64,
        line_ending_converted=False,
        final_mode=0o644,
        mode_source="new_default",
    )


def test_local_file_entry_is_frozen():
    entry = make_local_entry()
    with pytest.raises(FrozenInstanceError):
        entry.raw_size = 5


def test_remote_file_entry_allows_unknown_hash():
    entry = RemoteFileEntry(relative_path="src/main.cpp", size=4, sha256=None, mode=0o640)
    assert entry.sha256 is None
    assert entry.mode == 0o640


def test_plan_item_defaults_are_safe_for_new_files():
    item = SyncPlanItem(relative_path="src/main.cpp", kind="add", local=make_local_entry())
    assert item.remote_size is None
    assert item.remote_sha256 is None
    assert item.final_mode == 0o644
    assert item.mode_source == "new_default"
    assert item.blocked_reason is None


def test_plan_and_result_use_independent_default_collections():
    first = SyncPlan(
        status="ready",
        items={},
        window=[],
        deferred=[],
        conflicts=[],
        summary={},
        confirm_digest="digest",
        has_more=False,
    )
    second = SyncPlan(
        status="ready",
        items={},
        window=[],
        deferred=[],
        conflicts=[],
        summary={},
        confirm_digest="digest",
        has_more=False,
    )
    first.conflicts.append({"kind": "link"})

    result = SyncResult(success=False, status="failed", operation="apply")

    assert second.conflicts == []
    assert result.summary["add"] == 0
    assert result.items == []
    assert result.warnings == []
