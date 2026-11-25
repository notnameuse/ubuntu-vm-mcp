"""开源卫生守护：源码、配置模板与文档不得含个人环境值。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ("192.168.1.7", "192.168.1.8", "/home/liu", "xiaoyouyou")
# 内嵌禁串字面量的守护文件必须登记在此，否则会自我命中
EXCLUDED = {"test_repo_hygiene.py"}
EXTRA_FILES = ("config.example.json", "README.md", "LICENSE", "verify.md")


def _scanned_files():
    """列出需要扫描的文件；排除登记过的守护文件与 docs 目录。"""
    for pattern in ("*.py", "tools/**/*.py", "scripts/**/*.py", "tests/**/*.py"):
        for path in ROOT.glob(pattern):
            if path.name not in EXCLUDED:
                yield path
    for name in EXTRA_FILES:
        yield ROOT / name


def test_no_personal_environment_values_in_sources():
    offenders = []
    for path in _scanned_files():
        # 守护文件被改名或移走时必须报警，否则守卫会静默变成空转
        assert path.exists(), f"受守护的文件缺失：{path.relative_to(ROOT)}"
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert not offenders, offenders
