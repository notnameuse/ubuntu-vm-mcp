import shlex
from pathlib import PurePosixPath


def vm_collect_log(ssh, file_path, timeout=None):
    rel_path = PurePosixPath(file_path)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return {"content": "", "truncated": False, "error": "file_path must stay inside project_root"}

    path = f"{ssh.project_root}/{rel_path.as_posix()}"
    stdout, stderr, code = ssh.exec(f"cat {shlex.quote(path)}", timeout)
    if code != 0:
        return {"content": "", "truncated": False, "error": stderr.strip()}
    content = stdout
    raw = content.encode()
    if len(raw) > ssh.log_max_bytes:
        return {"content": raw[: ssh.log_max_bytes].decode(errors="replace"), "truncated": True}
    return {"content": content, "truncated": False}
