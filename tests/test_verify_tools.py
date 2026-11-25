from unittest.mock import MagicMock, patch
from tools.verify_tools import vm_verify_stage6


def test_verify_stage6_calls_expected_targets_and_filter():
    ssh = MagicMock()
    ssh.exec.return_value = ("found\n", "", 0)

    with patch("tools.verify_tools.vm_build") as mock_build, patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 3, "passed": 3, "failed": 0, "failed_cases": [], "full_output": ""}
        result = vm_verify_stage6(ssh)

    targets = mock_build.call_args[1]["targets"]
    regex = mock_ctest.call_args[1]["filter"]
    assert "vision_gui" in targets
    assert "test_camera_provider" in targets
    assert "test_main_window_camera_state" in targets
    assert "test_image_convert" in targets
    assert "test_inspection_worker" in targets
    assert "test_main_window_camera_state" in regex
    assert result["overall"] == "passed"


def test_verify_stage6_marks_missing_device_as_not_found():
    ssh = MagicMock()
    ssh.exec.return_value = ("not_found\n", "", 0)

    with patch("tools.verify_tools.vm_build") as mock_build, patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 3, "passed": 3, "failed": 0, "failed_cases": [], "full_output": ""}
        result = vm_verify_stage6(ssh)

    assert result["steps"][0]["detail"] == "/dev/video0 not found (non-blocking)"


def test_verify_stage6_runs_direct_binary_checks_before_ctest():
    ssh = MagicMock()
    ssh.exec.side_effect = [
        ("found\n", "", 0),
        ("test_camera_provider ok\n", "", 0),
        ("test_main_window_camera_state ok\n", "", 0),
    ]

    with patch("tools.verify_tools.vm_build") as mock_build, patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 3, "passed": 3, "failed": 0, "failed_cases": [], "full_output": ""}
        vm_verify_stage6(ssh)

    commands = [call.args[0] for call in ssh.exec.call_args_list]
    assert any("test_camera_provider" in command for command in commands)
    assert any("test_main_window_camera_state" in command for command in commands)
