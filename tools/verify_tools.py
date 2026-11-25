import logging
import shlex
from pathlib import PurePosixPath

from tools.cmake_tools import vm_build
from tools.ctest_tools import vm_run_ctest

STAGE6_TARGETS = [
    "vision_gui",
    "test_camera_provider",
    "test_main_window_camera_state",
    "test_image_convert",
    "test_inspection_worker",
]

STAGE6_CTEST_FILTER = "test_main_window_camera_state|test_image_convert|test_inspection_worker"
STAGE6_DIRECT_TESTS = [
    "test_camera_provider",
    "test_main_window_camera_state",
]


def _configured_list(ssh, name, default):
    """读取列表型配置；类型不符或为空时回退默认值并告警，避免 MagicMock 等非列表真值被误用。返回副本。"""
    value = getattr(ssh, name, None)
    if isinstance(value, (list, tuple)) and value:
        return list(value)
    if value is not None:
        logging.warning("%s 类型不符合预期(%s)，已回退默认值", name, type(value).__name__)
    return list(default)


def _configured_text(ssh, name, default):
    """读取字符串型配置；类型不符或为空时回退默认值并告警。"""
    value = getattr(ssh, name, None)
    if isinstance(value, str) and value:
        return value
    if value is not None:
        logging.warning("%s 类型不符合预期(%s)，已回退默认值", name, type(value).__name__)
    return default


def _build_dir(ssh):
    candidate = PurePosixPath(getattr(ssh, "build_dir", "build") or "build")
    if candidate.is_absolute():
        candidate = PurePosixPath("build")
    return candidate.as_posix()


def _run_stage6_binary(ssh, binary_name):
    build_dir = f"{ssh.project_root}/{_build_dir(ssh)}"
    command = f"cd {shlex.quote(build_dir)} && ./{binary_name}"
    stdout, stderr, code = ssh.exec(command, None)
    detail = (stdout + stderr).strip()
    return code == 0, detail if detail else "ok"


def vm_verify_stage6(ssh):
    steps = []
    # 目标与过滤式优先取配置，让同类工程不改代码就能复用该工具
    targets = _configured_list(ssh, "verify_targets", STAGE6_TARGETS)
    direct_tests = _configured_list(ssh, "verify_direct_tests", STAGE6_DIRECT_TESTS)
    ctest_filter = _configured_text(ssh, "verify_ctest_filter", STAGE6_CTEST_FILTER)

    out, _, _ = ssh.exec("test -e /dev/video0 && echo found || echo not_found", None)
    found = out.strip() == "found"
    steps.append({
        "name": "check_video_device",
        "passed": True,
        "detail": "/dev/video0 found" if found else "/dev/video0 not found (non-blocking)",
    })

    build_result = vm_build(ssh, targets=targets)
    steps.append({
        "name": "build_targets",
        "passed": build_result["success"],
        "detail": "ok" if build_result["success"] else "\n".join(build_result["error_lines"]),
    })
    if not build_result["success"]:
        return {"steps": steps, "overall": "failed"}

    for binary_name in direct_tests:
        passed, detail = _run_stage6_binary(ssh, binary_name)
        steps.append({
            "name": f"run_{binary_name}",
            "passed": passed,
            "detail": detail,
        })
        if not passed:
            return {"steps": steps, "overall": "failed"}

    ctest_result = vm_run_ctest(ssh, filter=ctest_filter)
    ctest_passed = ctest_result.get("exit_code", 0) == 0 and ctest_result["failed"] == 0
    steps.append({
        "name": "regression_tests",
        "passed": ctest_passed,
        "detail": f"{ctest_result['passed']}/{ctest_result['total']} passed",
    })

    return {"steps": steps, "overall": "passed" if ctest_passed else "partial"}
