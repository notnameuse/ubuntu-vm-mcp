def vm_cmake_configure(ssh, extra_args="", timeout=None):
    cmd = f"cd {ssh.project_root} && cmake -S . -B build"
    if extra_args:
        cmd = f"{cmd} {extra_args}"
    stdout, stderr, code = ssh.exec(cmd, timeout)
    text = stdout + stderr
    errors = [ln for ln in text.splitlines() if "error:" in ln.lower()]
    return {
        "success": code == 0,
        "summary": f"exit_code={code}",
        "errors": errors,
        "full_output": text,
    }


def vm_build(ssh, targets=None, jobs=4, timeout=None):
    target_flags = ""
    if targets:
        target_flags = " " + " ".join([f"--target {t}" for t in targets])
    cmd = f"cd {ssh.project_root} && cmake --build build{target_flags} -j{jobs}"
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
