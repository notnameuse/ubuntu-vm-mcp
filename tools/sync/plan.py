"""本地与远端索引的差异分类、执行窗口和确认摘要。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from pathlib import PurePosixPath

from tools.sync.config import TEMP_FILE_PATTERN, SyncConfig
from tools.sync.models import (
    LocalFileEntry,
    SyncPlan,
    SyncPlanItem,
    default_summary,
)
from tools.sync.remote_index import RemoteIndex


TEMP_FILE_RE = re.compile(TEMP_FILE_PATTERN)


def compute_confirm_digest(root_realpath: str, window: Sequence[SyncPlanItem]) -> str:
    """对远端根和执行窗口的路径、双哈希及权限计算规范 JSON SHA256。"""
    window_payload = []
    for item in window:
        if item.local is None:
            raise ValueError("confirm digest window items must have local files")
        window_payload.append(
            {
                "path": item.relative_path,
                "raw_sha256": item.local.raw_sha256,
                "upload_sha256": item.local.upload_sha256,
                "remote_sha256": item.remote_sha256,
                "mode": item.final_mode,
            }
        )
    payload = {"root_realpath": root_realpath, "window": window_payload}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_key(path: str) -> str:
    """生成仅用于冲突检测的 NFC + casefold 键，实际路径保持原样。"""
    return unicodedata.normalize("NFC", path).casefold()


def _find_path_conflicts(paths: Sequence[str]) -> list[dict]:
    """发现大小写或 Unicode 归一化后指向不同实际路径的冲突。"""
    seen: dict[str, str] = {}
    conflicts: list[dict] = []
    for path in sorted(set(paths)):
        key = _normalized_key(path)
        previous = seen.get(key)
        if previous is not None and previous != path:
            conflicts.append(
                {
                    "kind": "path_conflict",
                    "path": path,
                    "error": f"path collides with {previous} after NFC/casefold",
                }
            )
        else:
            seen[key] = path
    return conflicts


def _is_shell_script(local: LocalFileEntry) -> bool:
    """统一判断 shell 脚本后缀，大小写不敏感。"""
    return PurePosixPath(local.relative_path).suffix.lower() == ".sh"


def _failed_plan(
    local_entries: Sequence[LocalFileEntry],
    remote_index: RemoteIndex,
    message: str,
    local_errors: Sequence[dict],
) -> SyncPlan:
    """为远端索引不完整或校验失败创建全局阻断计划。"""
    items: dict[str, SyncPlanItem] = {}
    error_paths = [str(item.get("relative_path", "")) for item in [*local_errors, *remote_index.errors]]
    affected_paths = {
        local.relative_path
        for local in local_entries
        if any(
            not path or local.relative_path == path or local.relative_path.startswith(path.rstrip("/") + "/")
            for path in error_paths
        )
    }
    for local in local_entries:
        if local.relative_path not in affected_paths:
            continue
        remote = remote_index.files.get(local.relative_path)
        items[local.relative_path] = SyncPlanItem(
            relative_path=local.relative_path,
            kind="failed",
            local=local,
            remote_size=remote.size if remote is not None else None,
            remote_sha256=remote.sha256 if remote is not None else None,
            final_mode=local.final_mode,
            mode_source=local.mode_source,
            blocked_reason=message,
        )
    summary = default_summary()
    summary["failed"] = len(affected_paths) or len(error_paths) or 1
    return SyncPlan(
        status="failed",
        items=items,
        window=[],
        deferred=[],
        conflicts=list(remote_index.conflicts),
        summary=summary,
        confirm_digest=compute_confirm_digest(remote_index.root_realpath, []),
        has_more=False,
        root_realpath=remote_index.root_realpath,
        errors=[*local_errors, *remote_index.errors],
        remote_excluded_count=remote_index.remote_excluded_count,
    )


def build_sync_plan(
    local_entries: Sequence[LocalFileEntry],
    remote_index: RemoteIndex,
    config: SyncConfig,
    local_errors: Sequence[dict] = (),
) -> SyncPlan:
    """分类全部本地和远端路径，并生成确定性的单批执行窗口。"""
    if local_errors or remote_index.errors or remote_index.aborted:
        if local_errors:
            message = local_errors[0].get("error", "local scan failed")
        elif remote_index.errors:
            message = remote_index.errors[0].get("error", "remote scan failed")
        else:
            message = "remote scan aborted"
        return _failed_plan(local_entries, remote_index, message, local_errors)

    local_by_path = {entry.relative_path: entry for entry in local_entries}
    remote_files = {
        path: entry
        for path, entry in remote_index.files.items()
        if not TEMP_FILE_RE.fullmatch(PurePosixPath(path).name)
    }
    all_paths = list(local_by_path) + list(remote_files) + list(remote_index.directories)
    conflicts = list(remote_index.conflicts) + _find_path_conflicts(all_paths)
    items: dict[str, SyncPlanItem] = {}
    summary = default_summary()
    candidates: list[SyncPlanItem] = []
    has_failed = False

    for relative_path in sorted(local_by_path):
        local = local_by_path[relative_path]
        remote = remote_files.get(relative_path)
        if remote is None:
            kind = "add"
            blocked_reason = None
            if _is_shell_script(local):
                final_mode = 0o755
                mode_source = "shell_default"
            else:
                final_mode = local.final_mode
                mode_source = local.mode_source
        else:
            same_size = remote.size == local.upload_size
            if same_size and (not remote.hash_checked or remote.sha256 is None):
                kind = "failed"
                blocked_reason = "remote same-size hash was not verified"
                has_failed = True
            elif same_size and remote.sha256 == local.upload_sha256:
                if _is_shell_script(local) and remote.mode != 0o755:
                    kind = "overwrite"
                else:
                    kind = "unchanged"
                blocked_reason = None
            else:
                kind = "overwrite"
                blocked_reason = None
            if _is_shell_script(local):
                final_mode = 0o755
                mode_source = "shell_default"
            elif kind in ("overwrite", "unchanged"):
                final_mode = remote.mode
                mode_source = "remote_preserved"
            else:
                final_mode = local.final_mode
                mode_source = local.mode_source

        item = SyncPlanItem(
            relative_path=relative_path,
            kind=kind,
            local=local,
            remote_size=remote.size if remote is not None else None,
            remote_sha256=remote.sha256 if remote is not None else None,
            final_mode=final_mode,
            mode_source=mode_source,
            blocked_reason=blocked_reason,
        )
        items[relative_path] = item
        if kind in summary:
            summary[kind] += 1
        if kind in ("add", "overwrite"):
            candidates.append(item)

    for relative_path in sorted(set(remote_files) - set(local_by_path)):
        remote = remote_files[relative_path]
        items[relative_path] = SyncPlanItem(
            relative_path=relative_path,
            kind="remote_only",
            local=None,
            remote_size=remote.size,
            remote_sha256=remote.sha256,
            final_mode=remote.mode,
            mode_source="remote_only",
        )
        summary["remote_only"] += 1

    if has_failed:
        return SyncPlan(
            status="failed",
            items=items,
            window=[],
            deferred=[],
            conflicts=conflicts,
            summary=summary,
            confirm_digest=compute_confirm_digest(remote_index.root_realpath, []),
            has_more=False,
            root_realpath=remote_index.root_realpath,
            errors=[*local_errors, *remote_index.errors],
            remote_excluded_count=remote_index.remote_excluded_count,
        )
    if conflicts:
        return SyncPlan(
            status="blocked_conflict",
            items=items,
            window=[],
            deferred=[],
            conflicts=conflicts,
            summary=summary,
            confirm_digest=compute_confirm_digest(remote_index.root_realpath, []),
            has_more=False,
            root_realpath=remote_index.root_realpath,
            errors=[*local_errors, *remote_index.errors],
            remote_excluded_count=remote_index.remote_excluded_count,
        )

    candidates.sort(key=lambda item: item.relative_path)
    window = candidates[: config.sync_max_files_per_call]
    deferred = candidates[config.sync_max_files_per_call :]
    summary["deferred"] = len(deferred)
    return SyncPlan(
        status="ready" if window else "no_changes",
        items=items,
        window=window,
        deferred=deferred,
        conflicts=conflicts,
        summary=summary,
        confirm_digest=compute_confirm_digest(remote_index.root_realpath, window),
        has_more=bool(deferred),
        root_realpath=remote_index.root_realpath,
        errors=[*local_errors, *remote_index.errors],
        remote_excluded_count=remote_index.remote_excluded_count,
    )
