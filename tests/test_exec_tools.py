from unittest.mock import MagicMock
from tools.exec_tools import vm_exec, vm_run_project_command


def mk_ssh():
    ssh = MagicMock()
    ssh.project_root = "/home/ubuntu/project"
    ssh.build_dir = "build"
    return ssh


def test_exec_blocks_empty_command():
    ssh = mk_ssh()
    result = vm_exec(ssh, "   ")
    assert result["blocked"] is True
    ssh.exec.assert_not_called()


def test_exec_runs_arbitrary_shell_command():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok", "", 0)

    result = vm_exec(ssh, "ls; pwd")

    assert result["blocked"] is False
    assert result["exit_code"] == 0
    ssh.exec.assert_called_once_with("cd /home/ubuntu/project && ls; pwd", None)


def test_exec_retries_project_local_script_when_not_found():
    ssh = mk_ssh()
    ssh.exec.side_effect = [("", "startup.sh: not found\n", 127), ("started\n", "", 0)]

    result = vm_exec(ssh, "startup.sh --dry-run")

    assert result["blocked"] is False
    assert result["exit_code"] == 0
    commands = [call.args[0] for call in ssh.exec.call_args_list]
    assert commands == [
        "cd /home/ubuntu/project && startup.sh --dry-run",
        "cd /home/ubuntu/project && ./startup.sh --dry-run",
    ]


def test_exec_keeps_interpreter_invocation_unchanged():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok\n", "", 0)

    result = vm_exec(ssh, "bash startup.sh")

    assert result["blocked"] is False
    ssh.exec.assert_called_once_with("cd /home/ubuntu/project && bash startup.sh", None)


def test_exec_passes_stdin_text_through():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok\n", "", 0)

    result = vm_exec(ssh, "bash startup.sh", stdin_text="3")

    assert result["blocked"] is False
    ssh.exec.assert_called_once_with("cd /home/ubuntu/project && bash startup.sh", None, stdin_text="3")


def test_exec_allows_common_build_and_script_commands():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok\n", "", 0)

    commands = ("cmake --build build", "make -C build", "sh startup.sh")
    for command in commands:
        result = vm_exec(ssh, command)
        assert result["blocked"] is False

    assert [call.args[0] for call in ssh.exec.call_args_list] == [
        "cd /home/ubuntu/project && cmake --build build",
        "cd /home/ubuntu/project && make -C build",
        "cd /home/ubuntu/project && sh startup.sh",
    ]


def test_exec_truncates_stdout():
    ssh = mk_ssh()
    ssh.exec.return_value = ("a" * 11000, "", 0)

    result = vm_exec(ssh, "python -c 'print(1)'")

    assert result["blocked"] is False
    assert result["stdout"].endswith("[truncated]")


def test_run_project_command_runs_script_from_project_root():
    ssh = mk_ssh()
    ssh.exec.side_effect = [("", "startup.sh: not found\n", 127), ("started\n", "", 0)]

    result = vm_run_project_command(ssh, "startup.sh --dry-run")

    assert result["blocked"] is False
    assert result["exit_code"] == 0
    commands = [call.args[0] for call in ssh.exec.call_args_list]
    assert commands == [
        "cd /home/ubuntu/project && startup.sh --dry-run",
        "cd /home/ubuntu/project && ./startup.sh --dry-run",
    ]


def test_run_project_command_runs_binary_from_relative_workdir():
    ssh = mk_ssh()
    ssh.exec.side_effect = [("", "vision_gui: not found\n", 127), ("gui ok\n", "", 0)]

    result = vm_run_project_command(ssh, "vision_gui --help", working_directory="build")

    assert result["blocked"] is False
    commands = [call.args[0] for call in ssh.exec.call_args_list]
    assert commands == [
        "cd /home/ubuntu/project/build && vision_gui --help",
        "cd /home/ubuntu/project/build && ./vision_gui --help",
    ]


def test_run_project_command_allows_absolute_working_directory():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok\n", "", 0)

    result = vm_run_project_command(ssh, "env", working_directory="/tmp/runtime")

    assert result["blocked"] is False
    ssh.exec.assert_called_once_with("cd /tmp/runtime && env", None)


def test_run_project_command_keeps_interpreter_invocation_unchanged():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok\n", "", 0)

    result = vm_run_project_command(ssh, "bash startup.sh", working_directory=".")

    assert result["blocked"] is False
    ssh.exec.assert_called_once_with("cd /home/ubuntu/project && bash startup.sh", None)


def test_run_project_command_passes_stdin_text_through():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok\n", "", 0)

    result = vm_run_project_command(ssh, "bash startup.sh", working_directory=".", stdin_text="9")

    assert result["blocked"] is False
    ssh.exec.assert_called_once_with("cd /home/ubuntu/project && bash startup.sh", None, stdin_text="9")


def test_run_project_command_handles_multilevel_script_path():
    ssh = mk_ssh()
    ssh.exec.return_value = ("ok\n", "", 0)

    result = vm_run_project_command(ssh, "tools/setup.sh")

    assert result["blocked"] is False
    cmd = ssh.exec.call_args[0][0]
    assert "tools/setup.sh" in cmd


def test_exec_retries_only_for_simple_commands():
    ssh = mk_ssh()
    ssh.exec.return_value = ("", "not found\n", 127)

    result = vm_exec(ssh, "startup.sh && echo ok")

    assert result["blocked"] is False
    ssh.exec.assert_called_once_with("cd /home/ubuntu/project && startup.sh && echo ok", None)


def test_run_project_command_truncates_stdout():
    ssh = mk_ssh()
    ssh.exec.return_value = ("b" * 11000, "", 0)

    result = vm_run_project_command(ssh, "startup.sh")

    assert result["blocked"] is False
    assert result["stdout"].endswith("[truncated]")
