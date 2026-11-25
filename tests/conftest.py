"""同步测试共用的配置、文件条目、计划和内存 SFTP 工厂。"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from tools.sync.config import SyncConfig
from tools.sync.models import (
    LocalFileEntry,
    RemoteFileEntry,
    SyncPlan,
)
from tools.sync.scanner import prepare_upload_bytes


@pytest.fixture
def anyio_backend():
    """固定 AnyIO 测试后端为 asyncio。"""
    return "asyncio"


def sha256(data: bytes) -> str:
    """返回字节内容的 SHA256 十六进制值。"""
    return hashlib.sha256(data).hexdigest()


def make_config(
    tmp_path: Path | None = None,
    local_project_root: Path | None = None,
    max_files: int = 100,
) -> SyncConfig:
    """创建隔离的同步配置，默认项目与状态目录均位于临时目录。"""
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="vm-sync-test-"))
    package_root = tmp_path / "package"
    project_root = local_project_root or tmp_path / "project"
    state_dir = package_root / "sync_state"
    package_root.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return SyncConfig(
        package_root=package_root,
        local_project_root=project_root.resolve(),
        project_root="/home/liu/project",
        state_dir=state_dir,
        ssh_host="192.168.1.7",
        ssh_port=22,
        ssh_user="liu",
        ssh_key_path="key",
        verify_host_keys=True,
        sync_excludes=(),
        sync_token_ttl_seconds=900,
        sync_scan_timeout_seconds=600,
        sync_transfer_timeout_seconds=300,
        sync_max_files_per_call=max_files,
        raw={},
    )


@dataclass
class RemoteFile:
    """内存 SFTP 中的文件内容和权限。"""

    data: bytes
    mode: int = 0o644


class FakeSFTP:
    """供同步测试使用的内存 SFTP，记录所有变更类操作。"""

    def __init__(self, files: dict[str, RemoteFile] | None = None, root: str = "/home/liu/project"):
        self.files = dict(files or {})
        self.symlinks: set[str] = set()
        self.specials: set[str] = set()
        self.listdir_fail: set[str] = set()
        self.emit_dot_entries = False
        self.writes = 0
        self.read_hook = None
        self.fail_posix_rename = False
        self.fail_posix_rename_on_call: int | None = None
        self.rename_calls = 0
        self.corrupt_temp_before_rename = False
        self.corrupt_after_rename = False
        self.fail_putfo = False
        self.mkdir_race_path: str | None = None
        self.mkdir_race_symlink = False
        self.root = posixpath.normpath(root)
        self._directories: set[str] = {""}
        self._directory_modes: dict[str, int] = {"": 0o755}
        self._channel = SimpleNamespace(timeouts=[], closed=False)
        self._channel.settimeout = self._channel.timeouts.append

    def _key(self, path: str | Path) -> str:
        """把绝对远端路径映射为相对于测试项目根的键。"""
        raw_path = str(path)
        if PureWindowsPath(raw_path).drive:
            raise ValueError(f"path escapes fake root: {path}")
        value = posixpath.normpath(raw_path)
        if ".." in PurePosixPath(value).parts:
            raise ValueError(f"path contains parent traversal: {path}")
        if value.startswith("/"):
            if value == self.root:
                return ""
            prefix = self.root.rstrip("/") + "/"
            if not value.startswith(prefix):
                raise ValueError(f"path escapes fake root: {path}")
            value = value[len(prefix) :]
        return value.lstrip("/")

    def _all_directories(self) -> set[str]:
        """从文件、链接和显式目录推导完整目录集合。"""
        directories = set(self._directories)
        directories.add("")
        for path in list(self.files) + list(self.symlinks):
            parent = PurePosixPath(path).parent
            while parent.as_posix() not in ("", "."):
                directories.add(parent.as_posix())
                parent = parent.parent
        return directories

    def normalize(self, path: str) -> str:
        """只规范化 POSIX 路径，不改变测试根目录。"""
        return posixpath.normpath(path)

    def get_channel(self):
        """返回可记录超时设置的通道替身。"""
        return self._channel

    def close(self) -> None:
        """内存 SFTP 无需释放资源。"""
        return None

    def lstat(self, path: str):
        """返回目录、普通文件或符号链接的属性，不跟随链接。"""
        key = self._key(path)
        if key in self.symlinks:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_size=0)
        if key in self.specials:
            return SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_size=0)
        if key in self._all_directories():
            mode = self._directory_modes.get(key, 0o755)
            return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_size=0)
        if key in self.files:
            remote = self.files[key]
            return SimpleNamespace(st_mode=stat.S_IFREG | remote.mode, st_size=len(remote.data))
        raise FileNotFoundError(path)

    def listdir_attr(self, path: str):
        """列出目录下一层条目，符号链接只返回链接属性。"""
        key = self._key(path)
        if key in self.listdir_fail:
            raise OSError("listdir failed")
        if key not in self._all_directories():
            raise FileNotFoundError(path)
        prefix = "" if not key else key.rstrip("/") + "/"
        names: set[str] = set()
        for candidate in list(self.files) + list(self.symlinks) + list(self.specials) + list(self._all_directories()):
            if not candidate or candidate == key or not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            if remainder and "/" not in remainder:
                names.add(remainder)
        attributes = []
        if self.emit_dot_entries:
            attributes.extend(
                [
                    SimpleNamespace(filename=".", st_mode=stat.S_IFDIR | 0o755, st_size=0),
                    SimpleNamespace(filename="..", st_mode=stat.S_IFDIR | 0o755, st_size=0),
                ]
            )
        for name in sorted(names):
            child = prefix + name
            attributes.append(
                SimpleNamespace(
                    filename=name,
                    st_mode=self.lstat(child).st_mode,
                    st_size=self.lstat(child).st_size,
                )
            )
        return attributes

    def open(self, path: str, mode: str = "rb"):
        """打开内存文件；读取前触发可选故障钩子。"""
        key = self._key(path)
        if "r" in mode:
            if key not in self.files:
                raise FileNotFoundError(path)
            if self.read_hook is not None:
                self.read_hook()
            return io.BytesIO(self.files[key].data)
        if "w" in mode:
            raise NotImplementedError("FakeSFTP writes must use put or putfo")
        raise ValueError(f"unsupported mode: {mode}")

    def put(self, path: str, data: bytes) -> None:
        """写入完整文件并记录一次变更，覆盖时保留原权限。"""
        key = self._key(path)
        mode = self.files[key].mode if key in self.files else 0o644
        self.files[key] = RemoteFile(data, mode)
        self.writes += 1

    def putfo(self, handle, path: str, file_size: int | None = None, confirm: bool = True, callback=None) -> None:
        """从文件句柄当前位置写入数据，可注入临时文件损坏和上传失败。"""
        key = self._key(path)
        data = handle.read()
        if callback is not None:
            callback(0, len(data))
        if self.fail_putfo:
            raise OSError("putfo failed")
        if self.corrupt_temp_before_rename and PurePosixPath(key).name.startswith(".vm_sync-"):
            data = b"corrupt"
        mode = self.files[key].mode if key in self.files else 0o644
        self.files[key] = RemoteFile(data, mode)
        self.writes += 1

    def chmod(self, path: str, mode: int) -> None:
        """修改内存文件或目录权限并记录一次变更。"""
        key = self._key(path)
        if key in self.files:
            self.files[key].mode = mode
        elif key in self._all_directories():
            self._directory_modes[key] = mode
        else:
            raise FileNotFoundError(path)
        self.writes += 1

    def posix_rename(self, source: str, target: str) -> None:
        """模拟原子替换；失败时保留原目标。"""
        self.rename_calls += 1
        if self.fail_posix_rename or self.fail_posix_rename_on_call == self.rename_calls:
            raise OSError("posix rename unsupported")
        source_key = self._key(source)
        target_key = self._key(target)
        if source_key not in self.files:
            raise FileNotFoundError(source)
        remote = self.files.pop(source_key)
        if self.corrupt_after_rename:
            remote = RemoteFile(b"corrupt", remote.mode)
        self.files[target_key] = remote
        self.writes += 1

    def remove(self, path: str) -> None:
        """删除指定内存文件并记录一次变更。"""
        key = self._key(path)
        if key not in self.files:
            raise FileNotFoundError(path)
        del self.files[key]
        self.writes += 1

    def mkdir(self, path: str, mode: int = 0o777) -> None:
        """创建单层目录；父目录不存在时与真实 SFTP 一样失败。"""
        key = self._key(path)
        if self.mkdir_race_path == key:
            self.mkdir_race_path = None
            if self.mkdir_race_symlink:
                self.symlinks.add(key)
            else:
                self.files[key] = RemoteFile(b"race", 0o644)
            raise FileExistsError(path)
        if key in self._all_directories():
            raise FileExistsError(path)
        parent = PurePosixPath(key).parent.as_posix()
        if parent == ".":
            parent = ""
        if parent not in self._all_directories():
            raise FileNotFoundError(path)
        self._directories.add(key)
        self._directory_modes[key] = mode
        self.writes += 1

    def read(self, path: str) -> bytes:
        """读取文件内容，供测试直接断言远端结果。"""
        if self.read_hook is not None:
            self.read_hook()
        return self.files[self._key(path)].data

    def mode(self, path: str) -> int:
        """返回文件权限位。"""
        return self.files[self._key(path)].mode


class FakeSSH:
    """返回同一个 FakeSFTP 的轻量 SSH 替身。"""

    def __init__(self, sftp: FakeSFTP):
        self.sftp = sftp

    def open_sftp(self, timeout_seconds: int = 300):
        """返回固定内存 SFTP，忽略超时参数。"""
        return self.sftp

    def get_transport(self):
        """返回 None，表示测试替身不需要 keepalive。"""
        return None

    def close(self) -> None:
        """无需清理资源。"""
        return None


def make_remote_entry(
    data: bytes,
    relative_path: str = "src/main.cpp",
    mode: int = 0o644,
) -> RemoteFileEntry:
    """创建带 SHA256 的远端文件索引条目。"""
    return RemoteFileEntry(relative_path=relative_path, size=len(data), sha256=sha256(data), mode=mode)


def make_local_entry(
    relative_path: str,
    raw_data: bytes,
    remote: FakeSFTP | None = None,
    tmp_path: Path | None = None,
) -> LocalFileEntry:
    """落盘本地文件并通过 scanner 生成原始/上传双哈希条目。"""
    config = make_config(tmp_path)
    path = config.local_project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw_data)
    prepared = prepare_upload_bytes(path, path.suffix.lower() == ".sh")
    if remote is not None:
        mode = 0o755 if path.suffix.lower() == ".sh" else 0o644
        remote.files[relative_path] = RemoteFile(prepared.data, mode)
    return LocalFileEntry(
        relative_path=relative_path,
        absolute_path=path,
        raw_size=len(raw_data),
        raw_sha256=sha256(raw_data),
        upload_size=prepared.size,
        upload_sha256=prepared.sha256,
        line_ending_converted=prepared.line_ending_converted,
        final_mode=0o755 if path.suffix.lower() == ".sh" else 0o644,
        mode_source="shell_default" if path.suffix.lower() == ".sh" else "new_default",
    )


def make_plan(
    *specs,
    remote: FakeSFTP | None = None,
    max_files: int = 100,
    tmp_path: Path | None = None,
) -> SyncPlan:
    """创建传输和令牌测试所需的确定性同步计划。"""
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="vm-sync-plan-"))
    config = make_config(tmp_path, max_files=max_files)
    values = list(specs) or ["main.cpp", b"new"]
    parsed: list[LocalFileEntry] = []
    index = 0
    while index < len(values):
        value = values[index]
        if isinstance(value, LocalFileEntry):
            parsed.append(value)
            index += 1
        elif isinstance(value, tuple):
            parsed.append(make_local_entry(value[0], value[1], tmp_path=tmp_path))
            index += 1
        elif isinstance(value, str):
            if index + 1 < len(values) and isinstance(values[index + 1], bytes):
                parsed.append(make_local_entry(value, values[index + 1], tmp_path=tmp_path))
                index += 2
            else:
                parsed.append(make_local_entry(value, b"new", tmp_path=tmp_path))
                index += 1
        else:
            raise TypeError(f"unsupported plan spec: {value!r}")

    from tools.sync.plan import build_sync_plan
    from tools.sync.remote_index import RemoteIndex

    remote_files: dict[str, RemoteFileEntry] = {}
    directories: set[str] = set()
    if remote is not None:
        for relative_path, remote_file in remote.files.items():
            remote_files[relative_path] = RemoteFileEntry(
                relative_path=relative_path,
                size=len(remote_file.data),
                sha256=sha256(remote_file.data),
                mode=remote_file.mode,
                hash_checked=True,
            )
            parent = PurePosixPath(relative_path).parent
            while parent.as_posix() not in ("", "."):
                directories.add(parent.as_posix())
                parent = parent.parent
    remote_index = RemoteIndex(
        root_realpath=remote.root if remote is not None else config.project_root,
        files=remote_files,
        directories=directories,
    )
    return build_sync_plan(parsed, remote_index, config)


def make_sync_tools(
    tmp_path: Path,
    remote: FakeSFTP | None = None,
    max_files: int = 100,
):
    """创建指向临时项目和状态目录的 SyncTools 实例。

    remote 参数标记调用方计划使用的基线对象；SyncTools 不持有 SSH，
    调用方仍需显式传入 FakeSSH(remote)。
    """
    from tools.sync_tools import SyncTools

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "a.cpp").write_bytes(b"a")
    (project / "src" / "b.cpp").write_bytes(b"b")
    package_root = tmp_path / "package"
    package_root.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "ssh_host": "192.168.1.7",
                "ssh_port": 22,
                "ssh_user": "liu",
                "ssh_key_path": "key",
                "verify_host_keys": True,
                "project_root": "/home/liu/project",
                "local_project_root": str(project),
                "sync_excludes": [],
                "sync_state_path": str(package_root / "sync_state"),
                "sync_token_ttl_seconds": 900,
                "sync_scan_timeout_seconds": 600,
                "sync_transfer_timeout_seconds": 300,
                "sync_max_files_per_call": max_files,
            }
        ),
        encoding="utf-8",
    )
    tools = SyncTools(config_path=config_path, package_root=package_root)
    return tools
