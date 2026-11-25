import shlex
from pathlib import PurePosixPath


def _blocked(reason):
    return {"blocked": True, "reason": reason, "exit_code": None, "stdout": "", "stderr": ""}


def _resolve_working_directory(project_root, working_directory):
    candidate = PurePosixPath(working_directory or ".")
    if candidate.as_posix() in ("", "."):
        return project_root
    if candidate.is_absolute():
        return candidate.as_posix()
    return f"{project_root}/{candidate.as_posix()}"


def _prefix_local_command(command):
    stripped = command.strip()
    if not stripped:
        return None

    for shell_operator in ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n", "\r", "\u2028", "\u2029"):
        if shell_operator in stripped:
            return None

    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return None

    if not tokens:
        return None

    executable = tokens[0]
    if executable.startswith(("./", "../", "/", "-")) or "/" in executable:
        return None

    return " ".join(shlex.quote(token) for token in [f"./{executable}", *tokens[1:]])


def _truncate_stdout(stdout):
    max_bytes = 10240
    raw = stdout.encode()
    if len(raw) > max_bytes:
        return raw[:max_bytes].decode(errors="replace") + "\n[truncated]"
    return stdout


def _ssh_exec(ssh, command, timeout, stdin_text=None):
    if stdin_text is None:
        return ssh.exec(command, timeout)
    return ssh.exec(command, timeout, stdin_text=stdin_text)


def _run_command(ssh, working_directory, command, timeout=None, stdin_text=None):
    stripped = command.strip()
    if not stripped:
        return _blocked("empty command")

    wrapped = f"cd {shlex.quote(working_directory)} && {stripped}"
    stdout, stderr, code = _ssh_exec(ssh, wrapped, timeout, stdin_text=stdin_text)

    fallback_command = _prefix_local_command(stripped)
    if code == 127 and fallback_command is not None:
        wrapped = f"cd {shlex.quote(working_directory)} && {fallback_command}"
        stdout, stderr, code = _ssh_exec(ssh, wrapped, timeout, stdin_text=stdin_text)

    return {"blocked": False, "exit_code": code, "stdout": _truncate_stdout(stdout), "stderr": stderr}


def vm_exec(ssh, command, timeout=None, stdin_text=None):
    return _run_command(ssh, ssh.project_root, command=command, timeout=timeout, stdin_text=stdin_text)


def vm_run_project_command(ssh, command, working_directory=".", timeout=None, stdin_text=None):
    return _run_command(
        ssh,
        _resolve_working_directory(ssh.project_root, working_directory),
        command=command,
        timeout=timeout,
        stdin_text=stdin_text,
    )
