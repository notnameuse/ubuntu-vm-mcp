from unittest.mock import MagicMock
from tools.log_tools import vm_collect_log


def test_collect_log_truncate():
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.log_max_bytes = 10
    ssh.exec.return_value = ("0123456789ABC", "", 0)
    result = vm_collect_log(ssh, "build/CMakeFiles/CMakeError.log")
    assert result["truncated"] is True
    assert len(result["content"].encode()) <= 10


def test_collect_log_rejects_path_traversal():
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.log_max_bytes = 10
    result = vm_collect_log(ssh, "../etc/passwd")
    assert "error" in result
    ssh.exec.assert_not_called()
