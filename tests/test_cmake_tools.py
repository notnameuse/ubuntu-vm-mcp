import shlex

from unittest.mock import MagicMock
from tools.cmake_tools import vm_cmake_configure, vm_build


def mk_ssh(stdout="", stderr="", code=0):
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.build_dir = "build"
    ssh.exec.return_value = (stdout, stderr, code)
    return ssh


def test_configure_success():
    ssh = mk_ssh(stdout="-- Configuring done\n")
    result = vm_cmake_configure(ssh)
    assert result["success"] is True


def test_configure_runs_from_build_directory_for_older_cmake():
    ssh = mk_ssh(stdout="-- Configuring done\n")

    vm_cmake_configure(ssh)

    cmd = ssh.exec.call_args[0][0]
    assert cmd == "cd /home/ubuntu/project && mkdir -p build && cd build && cmake .."


def test_build_parses_error_warning():
    ssh = mk_ssh(stdout="a.cpp:1: error: bad\na.cpp:2: warning: warn\n", code=1)
    result = vm_build(ssh, targets=["vision_gui"], jobs=2)
    assert result["success"] is False
    assert len(result["error_lines"]) == 1
    assert result["warning_count"] == 1


def test_build_runs_without_parallel_flags_by_default_for_older_cmake():
    ssh = mk_ssh(stdout="build ok\n")

    vm_build(ssh)

    cmd = ssh.exec.call_args[0][0]
    assert cmd == "cd /home/ubuntu/project && cmake --build build"


def test_configure_shell_quotes_extra_args():
    ssh = mk_ssh(stdout="-- Configuring done\n")
    extra_args = '-DNAME="value; touch /tmp/pwned"'
    vm_cmake_configure(ssh, extra_args=extra_args)
    cmd = ssh.exec.call_args[0][0]
    assert shlex.quote("-DNAME=value; touch /tmp/pwned") in cmd


def test_build_shell_quotes_target_names():
    ssh = mk_ssh(stdout="build ok\n")
    target_name = "vision_gui; touch /tmp/pwned"
    vm_build(ssh, targets=[target_name], jobs=2)
    cmd = ssh.exec.call_args[0][0]
    assert f"--target {shlex.quote(target_name)}" in cmd
    assert cmd.endswith(" -- -j2")


def test_configure_extra_args_placed_after_cmake_sourcedir():
    ssh = mk_ssh(stdout="-- Configuring done\n")
    vm_cmake_configure(ssh, extra_args="-DFOO=bar")
    cmd = ssh.exec.call_args[0][0]
    assert cmd == "cd /home/ubuntu/project && mkdir -p build && cd build && cmake .. -DFOO=bar"


def test_configure_multilevel_build_directory():
    ssh = mk_ssh(stdout="-- Configuring done\n")
    ssh.build_dir = "build/release"
    vm_cmake_configure(ssh)
    cmd = ssh.exec.call_args[0][0]
    assert cmd == "cd /home/ubuntu/project && mkdir -p build/release && cd build/release && cmake ../.."
