import shlex
from pathlib import PurePosixPath


def _build_dir(ssh):
    return PurePosixPath(getattr(ssh, "build_dir", "build") or "build").as_posix()


def _source_dir_from_build_dir(build_dir):
    parts = [part for part in PurePosixPath(build_dir).parts if part not in ("", ".")]
    if not parts:
        return "."
    return "/".join(".." for _ in parts)


def vm_cmake_configure(ssh, extra_args="", timeout=None):
    build_dir = _build_dir(ssh)
    source_dir = _source_dir_from_build_dir(build_dir)
    cmd = (
        f"cd {shlex.quote(ssh.project_root)} && mkdir -p {shlex.quote(build_dir)} && "
        f"cd {shlex.quote(build_dir)} && cmake {shlex.quote(source_dir)}"
    )
    if extra_args:
        try:
            quoted_args = " ".join(shlex.quote(token) for token in shlex.split(extra_args, posix=True))
        except ValueError as error:
            return {
                "success": False,
                "summary": f"invalid_extra_args: {error}",
                "errors": [str(error)],
                "full_output": "",
            }
        cmd = f"{cmd} {quoted_args}"
    stdout, stderr, code = ssh.exec(cmd, timeout)
    text = stdout + stderr
    errors = [ln for ln in text.splitlines() if "error:" in ln.lower()]
    return {
        "success": code == 0,
        "summary": f"exit_code={code}",
        "errors": errors,
        "full_output": text,
    }


def vm_build(ssh, targets=None, jobs=None, timeout=None):
    build_dir = _build_dir(ssh)
    target_flags = ""
    if targets:
        target_flags = " " + " ".join([f"--target {shlex.quote(t)}" for t in targets])
    build_tool_flags = ""
    if jobs is not None and jobs > 0:
        build_tool_flags = f" -- -j{jobs}"
    cmd = (
        f"cd {shlex.quote(ssh.project_root)} && cmake --build {shlex.quote(build_dir)}"
        f"{target_flags}{build_tool_flags}"
    )
    stdout, stderr, code = ssh.exec(cmd, timeout)
    text = stdout + stderr
    error_lines = [ln for ln in text.splitlines() if "error:" in ln.lower()]
    warning_count = sum(1 for ln in text.splitlines() if "warning:" in ln.lower())
    return {
        "success": code == 0,
        "error_lines": error_lines,
        "warning_count": warning_count,
        "full_output": text,
    }
