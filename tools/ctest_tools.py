import re


def vm_run_ctest(ssh, filter=None, verbose=False, timeout=None):
    cmd = f"cd {ssh.project_root} && ctest --test-dir build --output-on-failure"
    if filter:
        cmd += f" -R \"{filter}\""
    if verbose:
        cmd += " -V"

    stdout, stderr, _ = ssh.exec(cmd, timeout)
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
    else:
        total = passed = failed = 0

    failed_cases = []
    for m in re.finditer(r"Test #\d+: (\S+) .*\*\*\*Failed", text):
        failed_cases.append({"name": m.group(1), "output": ""})

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "failed_cases": failed_cases,
        "full_output": text,
    }
