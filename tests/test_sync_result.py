import json

import pytest

from tools.sync.result import DETAIL_BUDGET_BYTES, make_result, paginate_items


def test_make_result_contains_uniform_fields():
    result = make_result(True, "ready", "preview", token="t")

    assert result["success"] is True
    assert result["status"] == "ready"
    assert result["operation"] == "preview"
    assert result["token"] == "t"
    assert result["summary"]["add"] == 0
    assert result["items"] == []
    assert result["warnings"] == []


def test_paginate_limits_items_and_returns_next_offset():
    items = [{"path": f"file-{index}.cpp"} for index in range(5)]

    page, next_offset = paginate_items(items, 0, 2)

    assert page == items[:2]
    assert next_offset == 2


def test_paginate_respects_64k_budget():
    items = [{"path": "a.cpp", "blob": "x" * (40 * 1024)}, {"path": "b.cpp", "blob": "y" * (40 * 1024)}]

    page, next_offset = paginate_items(items, 0, 100)

    assert len(page) == 1
    assert next_offset == 1
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= DETAIL_BUDGET_BYTES


def test_paginate_rejects_invalid_offset():
    with pytest.raises(ValueError, match="offset"):
        paginate_items([], 1, 1)
