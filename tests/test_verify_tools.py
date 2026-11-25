import logging
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


def test_verify_stage6_uses_configured_targets_and_filter():
    """配置中给出目标列表与过滤式时，必须使用配置值而不是默认常量。"""
    ssh = MagicMock()
    ssh.exec.return_value = ("found\n", "", 0)
    ssh.verify_targets = ["my_app"]
    ssh.verify_direct_tests = ["test_my_app"]
    ssh.verify_ctest_filter = "test_my_app"

    with patch("tools.verify_tools.vm_build") as mock_build, patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 1, "passed": 1, "failed": 0, "failed_cases": [], "full_output": ""}
        result = vm_verify_stage6(ssh)

    assert mock_build.call_args[1]["targets"] == ["my_app"]
    assert mock_ctest.call_args[1]["filter"] == "test_my_app"
    commands = [call.args[0] for call in ssh.exec.call_args_list]
    assert any("./test_my_app" in command for command in commands)
    assert result["overall"] == "passed"


def test_verify_stage6_falls_back_when_config_type_is_wrong():
    """配置类型不对或为空时必须回退默认常量，不能把非列表真值当目标使用。"""
    ssh = MagicMock()
    ssh.exec.return_value = ("found\n", "", 0)
    ssh.verify_targets = "not-a-list"
    ssh.verify_ctest_filter = 123
    ssh.verify_direct_tests = {"bad": "type"}

    with patch("tools.verify_tools.vm_build") as mock_build, patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 3, "passed": 3, "failed": 0, "failed_cases": [], "full_output": ""}
        result = vm_verify_stage6(ssh)

    assert "vision_gui" in mock_build.call_args[1]["targets"]
    assert "test_main_window_camera_state" in mock_ctest.call_args[1]["filter"]

    # dict 类型的非法值不得被强转成目标名去执行；步骤名仍应为默认常量的组合
    commands = [call.args[0] for call in ssh.exec.call_args_list]
    assert not any("./bad" in command for command in commands)
    assert [step["name"] for step in result["steps"]] == [
        "check_video_device",
        "build_targets",
        "run_test_camera_provider",
        "run_test_main_window_camera_state",
        "regression_tests",
    ]


def test_verify_config_invalid_type_warns_without_leaking_value(caplog):
    """类型不对要告警，且告警里只出现键名与类型名，不能带出配置值原文。"""
    ssh = MagicMock()
    ssh.exec.return_value = ("found\n", "", 0)
    ssh.verify_targets = ["secret_target"]
    ssh.verify_direct_tests = None
    ssh.verify_ctest_filter = 123

    with caplog.at_level(logging.WARNING), \
         patch("tools.verify_tools.vm_build") as mock_build, \
         patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 1, "passed": 1, "failed": 0, "failed_cases": [], "full_output": ""}
        vm_verify_stage6(ssh)

    messages = [record.getMessage() for record in caplog.records]
    assert any("verify_ctest_filter" in text and "类型不符合预期(int)" in text for text in messages)
    assert not any("verify_targets" in text for text in messages)
    assert not any("secret_target" in text for text in messages)


def test_verify_config_empty_value_warns_as_empty(caplog):
    """空列表/空串要报“为空”，不能误报为类型不对；同时仍回退默认目标。"""
    ssh = MagicMock()
    ssh.exec.return_value = ("found\n", "", 0)
    ssh.verify_targets = []
    ssh.verify_direct_tests = []
    ssh.verify_ctest_filter = ""

    with caplog.at_level(logging.WARNING), \
         patch("tools.verify_tools.vm_build") as mock_build, \
         patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 3, "passed": 3, "failed": 0, "failed_cases": [], "full_output": ""}
        vm_verify_stage6(ssh)

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert all("为空" in text for text in messages)
    assert "vision_gui" in mock_build.call_args[1]["targets"]


def test_verify_config_absent_value_does_not_warn(caplog):
    """未配置（None）属正常状态，不得产生告警。"""
    ssh = MagicMock()
    ssh.exec.return_value = ("found\n", "", 0)
    ssh.verify_targets = None
    ssh.verify_direct_tests = None
    ssh.verify_ctest_filter = None

    with caplog.at_level(logging.WARNING), \
         patch("tools.verify_tools.vm_build") as mock_build, \
         patch("tools.verify_tools.vm_run_ctest") as mock_ctest:
        mock_build.return_value = {"success": True, "error_lines": [], "warning_count": 0, "full_output": ""}
        mock_ctest.return_value = {"total": 3, "passed": 3, "failed": 0, "failed_cases": [], "full_output": ""}
        vm_verify_stage6(ssh)

    assert caplog.records == []
