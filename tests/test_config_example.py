"""守护配置模板：必填键齐全、主机校验开启。个人环境值检查由 tests/test_repo_hygiene.py 统一负责。"""

import json
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


def test_example_is_valid_json_with_required_keys():
    """模板必须是合法 JSON，且包含 load_sync_config 与构建链要求的必填键。"""
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for key in (
        "ssh_host",
        "ssh_port",
        "ssh_user",
        "ssh_key_path",
        "project_root",
        "local_project_root",
        "verify_host_keys",
        "build_dir",
    ):
        assert key in raw


def test_example_enables_host_key_verification():
    """出厂默认必须开启主机密钥校验，不能默认关闭。"""
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert raw["verify_host_keys"] is True
