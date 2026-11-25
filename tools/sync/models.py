"""同步功能使用的不可变文件条目、计划和结果数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LocalFileEntry:
    """本地普通文件的原始信息、上传信息和最终权限。"""

    relative_path: str
    absolute_path: Path
    raw_size: int
    raw_sha256: str
    upload_size: int
    upload_sha256: str
    line_ending_converted: bool
    final_mode: int
    mode_source: str


@dataclass(frozen=True)
class RemoteFileEntry:
    """远端普通文件的索引信息；哈希未知时允许为 None。

    hash_checked 表示有本地对应项的远端文件是否已完成 SHA256 校验。
    False 表示校验失败并要求 Task 5/7 阻断，不能按未变化或正常覆盖放行。
    """

    relative_path: str
    size: int
    sha256: str | None
    mode: int
    hash_checked: bool = True


@dataclass(frozen=True)
class SyncPlanItem:
    """单个路径的差异分类和执行所需信息。"""

    relative_path: str
    kind: str
    local: LocalFileEntry | None
    remote_size: int | None = None
    remote_sha256: str | None = None
    final_mode: int = 0o644
    mode_source: str = "new_default"
    blocked_reason: str | None = None


@dataclass
class SyncPlan:
    """一次预览生成的完整分类、执行窗口和确认摘要。"""

    status: str
    items: dict[str, SyncPlanItem]
    window: list[SyncPlanItem]
    deferred: list[SyncPlanItem]
    conflicts: list[dict]
    summary: dict[str, int]
    confirm_digest: str
    has_more: bool
    root_realpath: str = ""
    errors: list[dict] = field(default_factory=list)
    remote_excluded_count: int = 0


def default_summary() -> dict[str, int]:
    """创建统一的差异统计字典，避免可变默认参数共享。"""
    return {
        "add": 0,
        "overwrite": 0,
        "unchanged": 0,
        "remote_only": 0,
        "deferred": 0,
        "failed": 0,
        "unprocessed": 0,
    }


@dataclass
class SyncResult:
    """五个同步工具统一返回的结构化结果。"""

    success: bool
    status: str
    operation: str
    token_state: str | None = None
    summary: dict[str, int] = field(default_factory=default_summary)
    items: list[dict] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    unprocessed: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    has_more: bool = False
    next_step: str | None = None
    local_project_root: str | None = None
    project_root: str | None = None
    effective_excludes: list[str] = field(default_factory=list)
    token: str | None = None
    expires_at: float | None = None
    confirm_digest: str | None = None
    root_realpath: str | None = None
    next_offset: int | None = None
