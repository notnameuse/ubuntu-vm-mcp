import shlex

from unittest.mock import MagicMock
from tools.ctest_tools import vm_run_ctest


def mk_ssh(output, code, stderr=""):
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.build_dir = "build"
    ssh.exec.return_value = (output, stderr, code)
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


def test_ctest_runs_from_build_directory():
    ssh = mk_ssh("100% tests passed, 0 tests failed out of 1", 0)
    vm_run_ctest(ssh)
    cmd = ssh.exec.call_args[0][0]
    assert cmd.startswith("cd /home/ubuntu/project && cd build && ctest")


def test_ctest_filter_is_quoted():
    ssh = mk_ssh("100% tests passed, 0 tests failed out of 1", 0)
    filter_value = 'test_a|test_b"; touch /tmp/pwned #'
    vm_run_ctest(ssh, filter=filter_value)
    cmd = ssh.exec.call_args[0][0]
    assert f"-R {shlex.quote(filter_value)}" in cmd


def test_ctest_nonzero_without_summary_is_failure():
    ssh = mk_ssh("", 8, stderr="ctest fatal error\n")
    result = vm_run_ctest(ssh)
    assert result["exit_code"] == 8
    assert result["failed"] == 1
    assert result["passed"] == 0


def test_ctest_zero_without_summary_is_failure():
    ssh = mk_ssh("No tests were found!!!\n", 0)
    result = vm_run_ctest(ssh)
    assert result["exit_code"] == 0
    assert result["failed"] == 1
    assert result["passed"] == 0
