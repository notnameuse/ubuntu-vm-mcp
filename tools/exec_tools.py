import re

BLOCKLIST = [
    r"rm\s+-rf\s+/",
    r"\bsudo\b",
    r"chmod\s+777",
    r"dd\s+if=",
    r"\bmkfs\b",
    r":\(\)\s*\{",
    r"(^|[;\s&|])cd(\s|$)",
    r"\.\./",
]


def vm_exec(ssh, command, timeout=None):
    for p in BLOCKLIST:
        if re.search(p, command):
            return {"blocked": True, "reason": f"blocked by {p}", "exit_code": None, "stdout": "", "stderr": ""}

    wrapped = f"cd {ssh.project_root} && {command}"
    stdout, stderr, code = ssh.exec(wrapped, timeout)

    max_bytes = 10240
    raw = stdout.encode()
    if len(raw) > max_bytes:
        stdout = raw[:max_bytes].decode(errors="replace") + "\n[truncated]"

    return {"blocked": False, "exit_code": code, "stdout": stdout, "stderr": stderr}
