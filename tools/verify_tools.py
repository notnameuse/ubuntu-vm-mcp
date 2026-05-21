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


def vm_verify_stage6(ssh):
    steps = []

    out, _, _ = ssh.exec("test -e /dev/video0 && echo found || echo not_found", None)
    found = "found" in out
    steps.append({
        "name": "check_video_device",
        "passed": True,
        "detail": "/dev/video0 found" if found else "/dev/video0 not found (non-blocking)",
    })

    build_result = vm_build(ssh, targets=STAGE6_TARGETS)
    steps.append({
        "name": "build_targets",
        "passed": build_result["success"],
        "detail": "ok" if build_result["success"] else "\n".join(build_result["error_lines"]),
    })
    if not build_result["success"]:
        return {"steps": steps, "overall": "failed"}

    ctest_result = vm_run_ctest(ssh, filter=STAGE6_CTEST_FILTER)
    steps.append({
        "name": "regression_tests",
        "passed": ctest_result["failed"] == 0,
        "detail": f"{ctest_result['passed']}/{ctest_result['total']} passed",
    })

    return {"steps": steps, "overall": "passed" if ctest_result["failed"] == 0 else "partial"}
