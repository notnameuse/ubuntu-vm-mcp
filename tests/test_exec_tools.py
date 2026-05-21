from unittest.mock import MagicMock
from tools.exec_tools import vm_exec


def test_exec_blocks_sudo():
    ssh = MagicMock()
    result = vm_exec(ssh, "sudo apt update")
    assert result["blocked"] is True


def test_exec_runs_normal():
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.exec.return_value = ("ok", "", 0)
    result = vm_exec(ssh, "ls")
    assert result["blocked"] is False
    assert result["exit_code"] == 0


def test_exec_blocks_cd_escape():
    ssh = MagicMock()
    result = vm_exec(ssh, "cd / && ls")
    assert result["blocked"] is True
    ssh.exec.assert_not_called()


def test_exec_blocks_and_cd_escape():
    ssh = MagicMock()
    result = vm_exec(ssh, "ls&&cd /tmp")
    assert result["blocked"] is True
    ssh.exec.assert_not_called()
