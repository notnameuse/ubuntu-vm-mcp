from unittest.mock import MagicMock
from tools.ctest_tools import vm_run_ctest


def mk_ssh(output, code):
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.exec.return_value = (output, "", code)
    return ssh


def test_ctest_all_pass():
    text = "100% tests passed, 0 tests failed out of 3"
    result = vm_run_ctest(mk_ssh(text, 0))
    assert result["total"] == 3
    assert result["failed"] == 0


def test_ctest_has_failures():
    text = "67% tests passed, 1 tests failed out of 3\n2/3 Test #2: test_image_convert ...***Failed"
    result = vm_run_ctest(mk_ssh(text, 1))
    assert result["failed"] == 1
    assert result["passed"] == 2


def test_ctest_filter_is_quoted():
    ssh = mk_ssh("100% tests passed, 0 tests failed out of 1", 0)
    vm_run_ctest(ssh, filter="test_a|test_b")
    cmd = ssh.exec.call_args[0][0]
    assert '-R "test_a|test_b"' in cmd
