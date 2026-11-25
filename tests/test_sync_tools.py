import json
from pathlib import Path

import pytest

from tests.conftest import FakeSFTP, FakeSSH, RemoteFile, make_sync_tools
from tools.sync.token_store import TokenStateError, TokenStore


def _write_project_file(tools, relative_path: str, data: bytes) -> Path:
    path = Path(tools.config_path).parent / "project" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_preview_does_not_modify_remote_and_apply_requires_matching_digest(tmp_path: Path):
    remote = FakeSFTP()
    tools = make_sync_tools(tmp_path, remote)

    preview = tools.preview(FakeSSH(remote))
    assert preview["status"] == "ready"
    assert remote.writes == 0

    rejected = tools.apply(FakeSSH(remote), preview["token"], "wrong")
    assert rejected["status"] == "rejected_stale"
    assert remote.writes == 0

    applied = tools.apply(FakeSSH(remote), preview["token"], preview["confirm_digest"])
    assert applied["success"] is True


def test_second_batch_requires_new_preview(tmp_path: Path):
    tools = make_sync_tools(tmp_path, max_files=1)
    ssh = FakeSSH(FakeSFTP())
    first = tools.preview(ssh)
    assert first["has_more"] is True
    assert tools.apply(ssh, first["token"], first["confirm_digest"])["status"] == "batch_applied"

    second = tools.preview(ssh)
    assert second["token"] != first["token"]
    assert second["summary"]["add"] == 1


def test_status_does_not_create_or_replace_token(tmp_path: Path):
    tools = make_sync_tools(tmp_path)
    ssh = FakeSSH(FakeSFTP())
    preview = tools.preview(ssh)

    tools.status(ssh)

    assert tools.apply(ssh, preview["token"], preview["confirm_digest"])["success"] is True


def test_config_change_rejected(tmp_path: Path):
    remote = FakeSFTP()
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)
    config = json.loads(Path(tools.config_path).read_text(encoding="utf-8"))
    config["sync_max_files_per_call"] = 7
    Path(tools.config_path).write_text(json.dumps(config), encoding="utf-8")

    result = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert result["status"] == "rejected_config_changed"
    assert remote.writes == 0


def test_blocked_plan_cannot_apply(tmp_path: Path):
    remote = FakeSFTP({"SRC/A.cpp": RemoteFile(b"x")})
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)

    preview = tools.preview(ssh)
    assert preview["status"] == "blocked_conflict"
    result = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert result["success"] is False
    assert remote.writes == 0


def test_detail_needs_no_ssh_and_respects_budget(tmp_path: Path):
    tools = make_sync_tools(tmp_path)
    preview = tools.preview(FakeSSH(FakeSFTP()))

    result = tools.detail(preview["token"], "add", offset=0, limit=1)

    assert result["success"] is True
    assert result["next_offset"] == 1
    assert len(result["items"]) == 1


def test_detail_unchanged_only_returns_statistics(tmp_path: Path):
    remote = FakeSFTP({"src/a.cpp": RemoteFile(b"a"), "src/b.cpp": RemoteFile(b"b")})
    tools = make_sync_tools(tmp_path, remote)
    preview = tools.preview(FakeSSH(remote))

    result = tools.detail(preview["token"], "unchanged")

    assert result["success"] is True
    assert result["items"] == []
    assert result["summary"]["unchanged"] == 2


def test_preview_reports_roots_and_excludes(tmp_path: Path):
    tools = make_sync_tools(tmp_path)

    preview = tools.preview(FakeSSH(FakeSFTP()))

    assert preview["local_project_root"].endswith("project")
    assert preview["project_root"] == "/home/user/project"
    assert ".git" in preview["effective_excludes"]


def test_empty_window_apply_is_no_changes(tmp_path: Path):
    remote = FakeSFTP({"src/a.cpp": RemoteFile(b"a"), "src/b.cpp": RemoteFile(b"b")})
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)

    result = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert result["status"] == "no_changes"
    assert result["success"] is True
    assert remote.writes == 0


def test_empty_window_wrong_digest_does_not_cancel_token(tmp_path: Path):
    remote = FakeSFTP({"src/a.cpp": RemoteFile(b"a"), "src/b.cpp": RemoteFile(b"b")})
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)

    rejected = tools.apply(ssh, preview["token"], "wrong")
    accepted = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert rejected["status"] == "rejected_stale"
    assert accepted["status"] == "no_changes"
    assert accepted["success"] is True


def test_apply_finishes_token_when_plan_rebuild_fails(tmp_path: Path):
    remote = FakeSFTP()
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)
    state_path = tmp_path / "package" / "sync_state" / "current.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["window"][0].pop("local")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = tools.apply(ssh, preview["token"], preview["confirm_digest"])
    store = TokenStore(tmp_path / "package" / "sync_state")

    assert result["success"] is False
    assert result["token_state"] == "consumed"
    with pytest.raises(TokenStateError, match="stale"):
        store.load(preview["token"])


def test_shell_script_second_preview_is_unchanged(tmp_path: Path):
    remote = FakeSFTP()
    tools = make_sync_tools(tmp_path, remote)
    _write_project_file(tools, "startup.sh", b"#!/bin/sh\r\necho ok\r\n")
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)
    applied = tools.apply(ssh, preview["token"], preview["confirm_digest"])
    assert applied["success"] is True

    second = tools.preview(ssh)

    assert second["status"] == "no_changes"
    assert second["summary"]["unchanged"] == 3


def test_unicode_and_space_names_round_trip(tmp_path: Path):
    remote = FakeSFTP()
    tools = make_sync_tools(tmp_path, remote)
    _write_project_file(tools, "目录/配置 file.cpp", b"data")
    ssh = FakeSSH(remote)

    preview = tools.preview(ssh)
    applied = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert applied["success"] is True
    assert remote.read("目录/配置 file.cpp") == b"data"


def test_remote_only_and_similar_temp_name_survive(tmp_path: Path):
    similar = ".vm_sync-nothex.tmp"
    remote = FakeSFTP({"remote-only.cpp": RemoteFile(b"keep"), similar: RemoteFile(b"keep")})
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)

    preview = tools.preview(ssh)
    applied = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert applied["success"] is True
    assert remote.read("remote-only.cpp") == b"keep"
    assert remote.read(similar) == b"keep"


def test_local_scan_error_fails_preview(tmp_path: Path, monkeypatch):
    tools = make_sync_tools(tmp_path)
    monkeypatch.setattr(
        "tools.sync_tools.scan_local",
        lambda config: ([], [{"relative_path": "secret.cpp", "error": "denied"}]),
    )

    preview = tools.preview(FakeSSH(FakeSFTP()))

    assert preview["status"] == "failed"
    assert preview["has_more"] is False
    assert preview["error"] == "denied"


def test_concurrent_preview_returns_busy(tmp_path: Path):
    tools = make_sync_tools(tmp_path)
    state_dir = tmp_path / "package" / "sync_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "claim.lock").write_text(
        json.dumps({"pid": 999, "token": "other", "acquired_at": 9e18}),
        encoding="utf-8",
    )

    preview = tools.preview(FakeSSH(FakeSFTP()))

    assert preview["success"] is False
    assert "busy" in preview["error"]


def test_host_key_warning_on_apply(tmp_path: Path):
    remote = FakeSFTP()
    tools = make_sync_tools(tmp_path, remote)
    config = json.loads(Path(tools.config_path).read_text(encoding="utf-8"))
    config["verify_host_keys"] = False
    Path(tools.config_path).write_text(json.dumps(config), encoding="utf-8")
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)

    result = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert result["success"] is True
    assert any("host key verification" in warning for warning in result["warnings"])


def test_heartbeat_failure_does_not_lose_applied_files(tmp_path: Path, monkeypatch):
    remote = FakeSFTP()
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)

    def broken_touch(self, token):
        raise OSError("disk full")

    monkeypatch.setattr(TokenStore, "touch", broken_touch)
    result = tools.apply(ssh, preview["token"], preview["confirm_digest"])

    assert result["success"] is True
    assert result["applied"] == ["src/a.cpp", "src/b.cpp"]
    assert any("heartbeat write failed" in warning for warning in result["warnings"])


def test_detail_failed_plan_returns_failure_semantics(tmp_path: Path):
    remote = FakeSFTP({"SRC/A.cpp": RemoteFile(b"x")})
    tools = make_sync_tools(tmp_path, remote)
    ssh = FakeSSH(remote)
    preview = tools.preview(ssh)

    detail = tools.detail(preview["token"], "add")

    assert detail["success"] is False
    assert detail["status"] == "blocked_conflict"
    assert detail["error"]
