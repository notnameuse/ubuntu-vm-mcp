import asyncio
import importlib
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


def import_server_module():
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_server_import_does_not_create_ssh_client():
    with patch("ssh_client.SSHClient") as mock_client:
        import_server_module()

    assert mock_client.call_count == 0
    sys.modules.pop("server", None)


def test_vm_check_connection_creates_client_on_demand():
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.exec.return_value = ("vm-host\n6.6.0\n", "", 0)

    with patch("ssh_client.SSHClient", return_value=mock_client) as mock_client_cls:
        server = import_server_module()
        result = server.vm_check_connection_tool()

    assert result["connected"] is True
    assert result["hostname"] == "vm-host"
    assert result["kernel"] == "6.6.0"
    mock_client_cls.assert_called_once_with()
    mock_client.__enter__.assert_called_once()
    mock_client.__exit__.assert_called_once()
    sys.modules.pop("server", None)


def test_vm_run_project_command_uses_ssh_client_on_demand():
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client

    with patch("ssh_client.SSHClient", return_value=mock_client) as mock_client_cls:
        server = import_server_module()
        with patch.object(server, "vm_run_project_command") as mock_run:
            mock_run.return_value = {"blocked": False, "exit_code": 0, "stdout": "ok\n", "stderr": ""}
            result = server.vm_run_project_command_tool("./vision_gui --help", working_directory="build")

    assert result["blocked"] is False
    mock_client_cls.assert_called_once_with()
    mock_run.assert_called_once_with(mock_client, command="./vision_gui --help", working_directory="build")
    mock_client.__enter__.assert_called_once()
    mock_client.__exit__.assert_called_once()
    sys.modules.pop("server", None)


def test_vm_exec_tool_passes_stdin_text():
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client

    with patch("ssh_client.SSHClient", return_value=mock_client) as mock_client_cls:
        server = import_server_module()
        with patch.object(server, "vm_exec") as mock_exec:
            mock_exec.return_value = {"blocked": False, "exit_code": 0, "stdout": "ok\n", "stderr": ""}
            result = server.vm_exec_tool("bash startup.sh", stdin_text="9")

    assert result["blocked"] is False
    mock_client_cls.assert_called_once_with()
    mock_exec.assert_called_once_with(mock_client, command="bash startup.sh", stdin_text="9")
    mock_client.__enter__.assert_called_once()
    mock_client.__exit__.assert_called_once()
    sys.modules.pop("server", None)


def test_sync_tools_are_registered_with_expected_annotations():
    server = import_server_module()
    registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

    assert registered["vm_sync_preview"].annotations.readOnlyHint is True
    assert registered["vm_sync_detail"].annotations.readOnlyHint is True
    assert registered["vm_sync_status"].annotations.readOnlyHint is True
    assert registered["vm_sync_apply"].annotations.readOnlyHint is False
    assert registered["vm_sync_apply"].annotations.destructiveHint is True
    assert registered["vm_sync_cancel"].annotations.readOnlyHint is False
    for name in (
        "vm_check_connection",
        "vm_cmake_configure",
        "vm_build",
        "vm_run_ctest",
        "vm_collect_log",
        "vm_verify_stage6",
        "vm_exec",
        "vm_run_project_command",
    ):
        assert name in registered
    sys.modules.pop("server", None)


def test_apply_description_requires_explicit_user_approval():
    server = import_server_module()
    registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

    description = registered["vm_sync_apply"].description
    assert "必须先展示 preview/detail 并获得用户明确批准，禁止自动串联执行" in description
    assert "确认摘要仅绑定计划内容" in description
    sys.modules.pop("server", None)


def test_sync_module_import_does_not_call_config_loader():
    sys.modules.pop("server", None)
    sys.modules.pop("tools.sync_tools", None)
    with patch("tools.sync.config.load_sync_config") as loader:
        import_server_module()

    assert loader.call_count == 0
    sys.modules.pop("server", None)


def test_invalid_sync_config_does_not_block_legacy_tools(tmp_path):
    from tools.sync.config import load_sync_config

    server = import_server_module()
    bad_config = tmp_path / "config.json"
    bad_config.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        load_sync_config(bad_config, tmp_path)

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.exec.return_value = ("vm-host\n6.6.0\n", "", 0)
    try:
        with patch("server.SSHClient", return_value=mock_client):
            result = server.vm_check_connection_tool()
    finally:
        sys.modules.pop("server", None)

    assert result["connected"] is True


def test_detail_and_cancel_do_not_create_ssh():
    server = import_server_module()
    with patch("server.SSHClient") as mock_client_cls:
        detail = asyncio.run(server.vm_sync_detail_tool("missing", "add"))
        cancel = asyncio.run(server.vm_sync_cancel_tool("missing"))

    assert detail["success"] is False
    assert cancel["success"] is False
    assert mock_client_cls.call_count == 0
    sys.modules.pop("server", None)


@pytest.mark.anyio
async def test_sync_preview_does_not_block_event_loop(monkeypatch):
    server = import_server_module()
    started = threading.Event()
    release = threading.Event()

    def blocking_preview(ssh):
        started.set()
        release.wait(2)
        return {"success": True, "status": "ready", "operation": "preview"}

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    monkeypatch.setattr(server.sync_tools, "vm_sync_preview", blocking_preview)
    monkeypatch.setattr(server, "SSHClient", MagicMock(return_value=mock_client))

    task = asyncio.create_task(server.vm_sync_preview_tool())
    assert await asyncio.to_thread(started.wait, 1)
    release.set()
    result = await asyncio.wait_for(task, 2)

    assert result["success"] is True
    sys.modules.pop("server", None)
