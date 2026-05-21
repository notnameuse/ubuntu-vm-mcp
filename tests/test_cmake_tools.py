from unittest.mock import MagicMock
from tools.cmake_tools import vm_cmake_configure, vm_build


def mk_ssh(stdout="", stderr="", code=0):
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.exec.return_value = (stdout, stderr, code)
    return ssh


def test_configure_success():
    ssh = mk_ssh(stdout="-- Configuring done\n")
    result = vm_cmake_configure(ssh)
    assert result["success"] is True


def test_build_parses_error_warning():
    ssh = mk_ssh(stdout="a.cpp:1: error: bad\na.cpp:2: warning: warn\n", code=1)
    result = vm_build(ssh, targets=["vision_gui"], jobs=2)
    assert result["success"] is False
    assert len(result["error_lines"]) == 1
    assert result["warning_count"] == 1
