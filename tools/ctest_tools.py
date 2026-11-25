import re
import shlex
from pathlib import PurePosixPath


def _build_dir(ssh):
    return PurePosixPath(getattr(ssh, "build_dir", "build") or "build").as_posix()


def vm_run_ctest(ssh, filter=None, verbose=False, timeout=None):
    build_dir = _build_dir(ssh)
    cmd = f"cd {shlex.quote(ssh.project_root)} && cd {shlex.quote(build_dir)} && ctest --output-on-failure"
    if filter:
        cmd += f" -R {shlex.quote(filter)}"
    if verbose:
        cmd += " -V"

    stdout, stderr, code = ssh.exec(cmd, timeout)
    text = stdout + stderr

    all_pass = re.search(r"100% tests passed, 0 tests failed out of (\d+)", text)
    partial = re.search(r"(\d+)% tests passed, (\d+) tests failed out of (\d+)", text)

    if all_pass:
        total = int(all_pass.group(1))
        passed, failed = total, 0
    elif partial:
        total = int(partial.group(3))
        failed = int(partial.group(2))
        passed = total - failed
    elif code != 0:
        total = 0
        passed = 0
        failed = 1
    else:
        total = 0
        passed = 0
        failed = 1

    failed_cases = []
    for m in re.finditer(r"Test #\d+: (\S+) .*\*\*\*Failed", text):
        failed_cases.append({"name": m.group(1), "output": ""})

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "exit_code": code,
        "failed_cases": failed_cases,
        "full_output": text,
    }
