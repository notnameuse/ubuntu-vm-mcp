"""同步工具统一结果、分页和响应裁剪。"""

from __future__ import annotations

import json
from collections.abc import Sequence

from tools.sync.models import default_summary


MAX_DETAIL_ITEMS = 100
DETAIL_BUDGET_BYTES = 64 * 1024


def make_result(
    success: bool,
    status: str,
    operation: str,
    **overrides,
) -> dict:
    """创建包含全部统一字段的结果字典。"""
    result = {
        "success": success,
        "status": status,
        "operation": operation,
        "token_state": None,
        "summary": default_summary(),
        "items": [],
        "applied": [],
        "failed": [],
        "unprocessed": [],
        "error": None,
        "warnings": [],
        "has_more": False,
        "next_step": None,
        "local_project_root": None,
        "project_root": None,
        "effective_excludes": [],
        "token": None,
        "expires_at": None,
        "confirm_digest": None,
        "root_realpath": None,
        "remote_excluded_count": 0,
        "next_offset": None,
    }
    result.update(overrides)
    return result


def paginate_items(items: Sequence[dict], offset: int, limit: int) -> tuple[list[dict], int | None]:
    """按条数和 UTF-8 64 KiB 预算分页，返回实际下一页偏移。"""
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if offset > len(items):
        raise ValueError("offset is out of range")
    actual_limit = min(max(1, limit), MAX_DETAIL_ITEMS)
    page: list[dict] = []
    for item in items[offset : offset + actual_limit]:
        candidate = page + [item]
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if page and len(encoded) > DETAIL_BUDGET_BYTES:
            break
        page.append(item)
    next_offset = offset + len(page)
    return page, next_offset if next_offset < len(items) else None
