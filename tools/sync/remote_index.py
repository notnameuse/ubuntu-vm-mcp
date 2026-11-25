"""远端 SFTP 文件枚举、哈希索引和路径安全校验。"""

from __future__ import annotations

import hashlib
import posixpath
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol, Sequence

import paramiko

from tools.sync.config import TEMP_FILE_PATTERN, SyncConfig, is_excluded
from tools.sync.models import LocalFileEntry, RemoteFileEntry


CHUNK_SIZE = 1024 * 1024
TEMP_FILE_RE = re.compile(TEMP_FILE_PATTERN)
LINK_CONFLICT = "link"
TYPE_CONFLICT = "type_conflict"
SFTP_ERRORS = (OSError, EOFError, TimeoutError, paramiko.SSHException, paramiko.SFTPError)
INDEX_ERRORS = SFTP_ERRORS + (ValueError, TypeError)


class SFTPLike(Protocol):
    """远端索引和路径校验所需的最小 SFTP 接口。"""

    def lstat(self, path: str) -> paramiko.SFTPAttributes: ...

    def listdir_attr(self, path: str) -> list[paramiko.SFTPAttributes]: ...

    def normalize(self, path: str) -> str: ...

    def open(self, path: str, mode: str) -> paramiko.SFTPFile: ...


class SSHLike(Protocol):
    """提供 SFTP 会话的最小 SSH 接口。"""

    def open_sftp(self, timeout_seconds: int = 300) -> paramiko.SFTPClient: ...


@dataclass
class RemoteIndex:
    """一次远端扫描的普通文件、目录、冲突和错误索引。"""

    root_realpath: str = ""
    files: dict[str, RemoteFileEntry] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)
    conflicts: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    remote_excluded_count: int = 0
    aborted: bool = False


def _check_deadline(deadline: float, relative_path: str) -> None:
    """在远端枚举和哈希读取循环中检查总截止时间。"""
    if time.monotonic() >= deadline:
        raise TimeoutError(f"remote scan timed out while reading {relative_path}")


def _join(root: str, relative_path: str) -> str:
    """用 POSIX 规则拼接远端根路径和相对路径。"""
    return posixpath.join(root, relative_path) if relative_path else root


def _affects_local_path(relative_path: str, local_paths: set[str]) -> bool:
    """判断远端链接或类型异常是否位于某个本地目标路径上。"""
    return any(path == relative_path or path.startswith(relative_path + "/") for path in local_paths)


def _add_conflict(index: RemoteIndex, kind: str, path: str, message: str) -> None:
    """按类型和路径去重追加冲突，避免重复枚举产生重复报告。"""
    if not any(item["kind"] == kind and item["path"] == path for item in index.conflicts):
        index.conflicts.append({"kind": kind, "path": path, "error": message})


def _hash_remote_file(sftp: SFTPLike, remote_path: str, relative_path: str, deadline: float) -> str:
    """分块读取远端普通文件并计算 SHA256，超时或读取失败直接抛出。"""
    digest = hashlib.sha256()
    with sftp.open(remote_path, "rb") as handle:
        while True:
            _check_deadline(deadline, relative_path)
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_filename(filename: str) -> None:
    """拒绝会破坏相对路径语义的 SFTP 条目名。"""
    if not isinstance(filename, str) or not filename or filename in (".", "..") or "/" in filename or "\x00" in filename:
        raise ValueError("invalid remote filename")


def build_remote_index(
    ssh: SSHLike,
    config: SyncConfig,
    local_entries: Sequence[LocalFileEntry],
    deadline: float,
) -> RemoteIndex:
    """枚举远端项目，只跟随普通目录并对有本地对应项的远端文件计算 SHA256。"""
    index = RemoteIndex()
    local_by_path = {entry.relative_path: entry for entry in local_entries}
    local_paths = set(local_by_path)
    excludes = config.effective_excludes
    try:
        sftp = ssh.open_sftp(timeout_seconds=config.sync_scan_timeout_seconds)
        root_attr = sftp.lstat(config.project_root)
        if stat.S_ISLNK(root_attr.st_mode) or not stat.S_ISDIR(root_attr.st_mode):
            raise ValueError("remote project root must be a real directory")
        root_realpath = sftp.normalize(config.project_root)
        normalized_root = PurePosixPath(root_realpath)
        if not normalized_root.is_absolute() or normalized_root == PurePosixPath("/"):
            raise ValueError("normalized remote root must be an absolute non-root POSIX path")
    except SFTP_ERRORS as exc:
        index.aborted = True
        index.errors.append({"relative_path": "", "error": str(exc)})
        return index
    except ValueError as exc:
        index.aborted = True
        index.errors.append({"relative_path": "", "error": str(exc)})
        return index

    index.root_realpath = root_realpath
    remote_links: set[str] = set()
    timed_out = False

    def mark_timeout(relative_path: str, exc: Exception) -> None:
        """只记录首次超时并标记整个扫描立即停止。"""
        nonlocal timed_out
        if not timed_out:
            index.errors.append({"relative_path": relative_path, "error": str(exc)})
            index.aborted = True
            timed_out = True

    def walk(remote_directory: str, relative_directory: str) -> None:
        """递归枚举单个远端目录，遇到链接或超时立即停止。"""
        if timed_out:
            return
        try:
            _check_deadline(deadline, relative_directory)
            attributes = sftp.listdir_attr(remote_directory)
        except TimeoutError as exc:
            mark_timeout(relative_directory, exc)
            return
        except SFTP_ERRORS as exc:
            index.errors.append({"relative_path": relative_directory, "error": str(exc)})
            return

        for attr in attributes:
            if timed_out:
                return
            relative_path = f"{relative_directory}/{attr.filename}" if relative_directory else attr.filename
            try:
                if attr.filename in (".", ".."):
                    continue
                _check_deadline(deadline, relative_path)
                _validate_filename(attr.filename)
                if TEMP_FILE_RE.fullmatch(attr.filename):
                    continue
                if is_excluded(relative_path, excludes):
                    index.remote_excluded_count += 1
                    continue
                mode = attr.st_mode
                if stat.S_ISLNK(mode):
                    remote_links.add(relative_path)
                    if relative_path in local_paths:
                        _add_conflict(index, TYPE_CONFLICT, relative_path, "remote target is a symbolic link")
                    elif _affects_local_path(relative_path, local_paths):
                        _add_conflict(index, LINK_CONFLICT, relative_path, "remote parent is a symbolic link")
                    continue
                if stat.S_ISDIR(mode):
                    index.directories.add(relative_path)
                    walk(_join(root_realpath, relative_path), relative_path)
                    continue
                if not stat.S_ISREG(mode):
                    if _affects_local_path(relative_path, local_paths):
                        _add_conflict(index, TYPE_CONFLICT, relative_path, "remote target is not a regular file")
                    continue

                size = int(attr.st_size)
                remote_hash = None
                hash_checked = False
                local = local_by_path.get(relative_path)
                if local is not None:
                    try:
                        remote_hash = _hash_remote_file(
                            sftp,
                            _join(root_realpath, relative_path),
                            relative_path,
                            deadline,
                        )
                        hash_checked = True
                    except TimeoutError as exc:
                        mark_timeout(relative_path, exc)
                    except SFTP_ERRORS as exc:
                        index.errors.append({"relative_path": relative_path, "error": str(exc)})
                index.files[relative_path] = RemoteFileEntry(
                    relative_path=relative_path,
                    size=size,
                    sha256=remote_hash,
                    mode=stat.S_IMODE(mode),
                    hash_checked=hash_checked,
                )
            except TimeoutError as exc:
                mark_timeout(relative_path, exc)
            except INDEX_ERRORS as exc:
                index.errors.append({"relative_path": relative_path, "error": str(exc)})

    walk(root_realpath, "")

    for local_path in sorted(local_paths):
        if local_path in index.directories:
            _add_conflict(index, TYPE_CONFLICT, local_path, "remote target is a directory")
        parts = PurePosixPath(local_path).parts
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            if parent in remote_links:
                _add_conflict(index, LINK_CONFLICT, parent, "remote parent is a symbolic link")
            if parent in index.files:
                _add_conflict(index, TYPE_CONFLICT, parent, "remote parent is not a directory")

    return index


def assert_safe_remote_path(
    sftp: SFTPLike,
    root_realpath: str,
    relative_path: str,
    allow_missing_leaf: bool = True,
) -> str:
    """校验远端目标及现有父目录不是链接，目标存在时必须是普通文件。"""
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("relative path must not be empty")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("relative path must be relative and contain no '..'")
    root = PurePosixPath(root_realpath)
    if not root.is_absolute() or root == PurePosixPath("/"):
        raise ValueError("root_realpath must be an absolute non-root POSIX path")

    root_attr = sftp.lstat(root_realpath)
    if stat.S_ISLNK(root_attr.st_mode) or not stat.S_ISDIR(root_attr.st_mode):
        raise ValueError("remote root must be a real directory")

    current = root_realpath
    for part in path.parts[:-1]:
        current = posixpath.join(current, part)
        try:
            attr = sftp.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(attr.st_mode):
            raise ValueError(f"remote parent is a link: {part}")
        if not stat.S_ISDIR(attr.st_mode):
            raise ValueError(f"remote parent is not a directory: {part}")

    target = posixpath.join(root_realpath, *path.parts)
    try:
        attr = sftp.lstat(target)
    except FileNotFoundError:
        if allow_missing_leaf:
            return target
        raise
    if stat.S_ISLNK(attr.st_mode):
        raise ValueError("remote target is a link")
    if not stat.S_ISREG(attr.st_mode):
        raise ValueError("remote target must be a regular file")
    return target
