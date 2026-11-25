"""同步配置校验、默认排除规则和配置指纹计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any


MCP_SYNC_PREFIX = "vm_sync"
TEMP_FILE_PATTERN = r"^\.vm_sync-[0-9a-f]{32}\.tmp$"

SEGMENT_NAMES = (
    ".git",
    ".qoder",
    ".vscode",
    "project_doc",
    "build",
    "out",
    "vision_deploy",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "compile_commands.json",
    "test.db",
)
SEGMENT_GLOBS = ("cmake-build-*",)
PATH_PREFIXES = ("docs/superpowers",)
SUFFIXES = (".o", ".a", ".so", ".exe")
DEFAULT_EXCLUDES = SEGMENT_NAMES + SEGMENT_GLOBS + PATH_PREFIXES + SUFFIXES


@dataclass(frozen=True)
class SyncConfig:
    """已解析的同步配置；预算字段始终保持正整数。"""

    package_root: Path
    local_project_root: Path
    project_root: str
    state_dir: Path
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_key_path: str
    verify_host_keys: bool
    sync_excludes: tuple[str, ...]
    sync_token_ttl_seconds: int
    sync_scan_timeout_seconds: int
    sync_transfer_timeout_seconds: int
    sync_max_files_per_call: int
    raw: dict[str, Any]

    @property
    def effective_excludes(self) -> tuple[str, ...]:
        """返回默认保护规则与用户追加规则的最终有序集合。"""
        return DEFAULT_EXCLUDES + self.sync_excludes


def _positive_int(raw: dict[str, Any], key: str, default: int) -> int:
    """读取正整数预算，拒绝布尔值和零/负值。"""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _validate_relative_rule(rule: str) -> None:
    """校验单条追加排除规则，防止规则本身表达越界路径。"""
    if not isinstance(rule, str) or not rule:
        raise ValueError("sync excludes must be non-empty POSIX paths")
    path = PurePosixPath(rule)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("sync excludes must be relative paths")


def _resolve_state_dir(package_root: Path, state_path: str) -> Path:
    """解析状态目录并拒绝越界或符号链接逃逸。"""
    if not isinstance(state_path, str) or not state_path:
        raise ValueError("sync state path must be a non-empty string")
    candidate = Path(state_path)
    if not candidate.is_absolute():
        candidate = package_root / candidate
    resolved_package = package_root.resolve()
    resolved_state = candidate.resolve()
    try:
        resolved_state.relative_to(resolved_package)
    except ValueError as exc:
        raise ValueError("sync state path must stay inside the package root") from exc
    return resolved_state


def load_sync_config(config_path: Path | str, package_root: Path | str) -> SyncConfig:
    """加载并校验同步配置；任何无效项都在工具调用时显式失败。"""
    path = Path(config_path)
    package = Path(package_root).resolve()
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid sync config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("sync config must be a JSON object")

    local_value = raw.get("local_project_root")
    if not isinstance(local_value, str) or not local_value:
        raise ValueError("local project root is required")
    local_root = Path(local_value).expanduser()
    if not local_root.is_absolute():
        raise ValueError("local project root must be absolute")
    local_root = local_root.resolve()
    if not local_root.is_dir():
        raise ValueError("local project root must be an existing directory")
    if local_root == Path(local_root.anchor) or local_root == Path.home().resolve():
        raise ValueError("local project root must not be a filesystem or user root")

    remote_root = raw.get("project_root")
    if not isinstance(remote_root, str) or not remote_root:
        raise ValueError("remote project root is required")
    remote_path = PurePosixPath(remote_root)
    if not remote_path.is_absolute() or remote_path == PurePosixPath("/") or ".." in remote_path.parts:
        raise ValueError("remote project root must be an absolute non-root POSIX path")

    excludes_value = raw.get("sync_excludes", [])
    if not isinstance(excludes_value, list):
        raise ValueError("sync_excludes must be a list")
    excludes = tuple(excludes_value)
    for rule in excludes:
        _validate_relative_rule(rule)

    state_dir = _resolve_state_dir(package, raw.get("sync_state_path", "sync_state"))
    ssh_host = raw.get("ssh_host")
    ssh_user = raw.get("ssh_user")
    ssh_key_path = raw.get("ssh_key_path")
    if not isinstance(ssh_host, str) or not ssh_host:
        raise ValueError("ssh_host is required")
    if not isinstance(ssh_user, str) or not ssh_user:
        raise ValueError("ssh_user is required")
    if not isinstance(ssh_key_path, str) or not ssh_key_path:
        raise ValueError("ssh_key_path is required")
    ssh_port = _positive_int(raw, "ssh_port", 22)
    verify_host_keys = raw.get("verify_host_keys", True)
    if not isinstance(verify_host_keys, bool):
        raise ValueError("verify_host_keys must be a boolean")

    return SyncConfig(
        package_root=package,
        local_project_root=local_root,
        project_root=remote_path.as_posix(),
        state_dir=state_dir,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        ssh_key_path=ssh_key_path,
        verify_host_keys=verify_host_keys,
        sync_excludes=excludes,
        sync_token_ttl_seconds=_positive_int(raw, "sync_token_ttl_seconds", 900),
        sync_scan_timeout_seconds=_positive_int(raw, "sync_scan_timeout_seconds", 600),
        sync_transfer_timeout_seconds=_positive_int(raw, "sync_transfer_timeout_seconds", 300),
        sync_max_files_per_call=_positive_int(raw, "sync_max_files_per_call", 100),
        raw=raw,
    )


def is_excluded(relative_path: str, excludes: tuple[str, ...]) -> bool:
    """按 POSIX 相对路径判断是否命中目录段、前缀或后缀排除规则。"""
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("relative path must be a non-empty POSIX path")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("relative path must not be absolute or contain '..'")
    parts = path.parts
    for rule in excludes:
        _validate_relative_rule(rule)
        rule_path = PurePosixPath(rule)
        if len(rule_path.parts) == 1 and rule in parts:
            return True
        if len(rule_path.parts) == 1 and any(fnmatchcase(part, rule) for part in parts[:-1]):
            return True
        if relative_path == rule or relative_path.startswith(rule.rstrip("/") + "/"):
            return True
        if rule not in SEGMENT_NAMES and rule.startswith(".") and path.name.endswith(rule):
            return True
    return False


def config_fingerprint(config: SyncConfig) -> str:
    """对影响同步安全和执行窗口的配置字段计算规范 JSON SHA256。"""
    payload = {
        "ssh_host": config.ssh_host,
        "ssh_port": config.ssh_port,
        "ssh_user": config.ssh_user,
        "ssh_key_path": config.ssh_key_path,
        "verify_host_keys": config.verify_host_keys,
        "project_root": config.project_root,
        "local_project_root": str(config.local_project_root),
        "sync_excludes": list(config.sync_excludes),
        "sync_max_files_per_call": config.sync_max_files_per_call,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
