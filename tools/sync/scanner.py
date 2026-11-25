"""本地项目扫描、原始/上传双哈希和 shell 脚本行尾转换。"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from tools.sync.config import SyncConfig, is_excluded
from tools.sync.models import LocalFileEntry


CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class PreparedUpload:
    """内存中准备好的上传字节及其大小、哈希和行尾转换标记。"""

    data: bytes
    size: int
    sha256: str
    line_ending_converted: bool


def prepare_upload_bytes(path: Path, is_shell_script: bool) -> PreparedUpload:
    """读取单个文件并在内存中生成上传字节，不修改本地文件。"""
    data = path.read_bytes()
    upload_data = data.replace(b"\r\n", b"\n") if is_shell_script else data
    return PreparedUpload(
        data=upload_data,
        size=len(upload_data),
        sha256=hashlib.sha256(upload_data).hexdigest(),
        line_ending_converted=upload_data != data,
    )


def _relative_path(root: Path, path: Path) -> str:
    """把本地路径转换为同步协议使用的 POSIX 相对路径。"""
    return path.relative_to(root).as_posix()


def _check_deadline(deadline: float, relative_path: str) -> None:
    """在扫描读取循环中检查总截止时间，超时立即失败。"""
    if time.monotonic() >= deadline:
        raise TimeoutError(f"local scan timed out while reading {relative_path}")


def _scan_file(path: Path, relative_path: str, deadline: float) -> LocalFileEntry:
    """分块计算普通文件双哈希，并为 shell 脚本保留转换后的上传字节。"""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("not a regular file")
    is_shell_script = path.suffix.lower() == ".sh"
    raw_hash = hashlib.sha256()
    raw_size = 0
    shell_chunks: list[bytes] = []
    with path.open("rb") as handle:
        while True:
            _check_deadline(deadline, relative_path)
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            raw_hash.update(chunk)
            raw_size += len(chunk)
            if is_shell_script:
                shell_chunks.append(chunk)
    after = path.lstat()
    if not stat.S_ISREG(after.st_mode):
        raise OSError("file changed type during scan")

    raw_sha256 = raw_hash.hexdigest()
    line_ending_converted = False
    if is_shell_script:
        raw_shell_data = b"".join(shell_chunks)
        upload_data = raw_shell_data.replace(b"\r\n", b"\n")
        upload_size = len(upload_data)
        upload_sha256 = hashlib.sha256(upload_data).hexdigest()
        line_ending_converted = upload_data != raw_shell_data
    else:
        upload_size = raw_size
        upload_sha256 = raw_sha256
    return LocalFileEntry(
        relative_path=relative_path,
        absolute_path=path,
        raw_size=raw_size,
        raw_sha256=raw_sha256,
        upload_size=upload_size,
        upload_sha256=upload_sha256,
        line_ending_converted=line_ending_converted,
        final_mode=0o755 if is_shell_script else 0o644,
        mode_source="shell_default" if is_shell_script else "new_default",
    )


def scan_local(config: SyncConfig) -> tuple[list[LocalFileEntry], list[dict]]:
    """扫描配置根目录，跳过链接、特殊文件和排除项并报告读取失败。"""
    root = config.local_project_root
    deadline = time.monotonic() + config.sync_scan_timeout_seconds
    entries: list[LocalFileEntry] = []
    errors: list[dict] = []
    excludes = config.effective_excludes

    def on_walk_error(exc: OSError) -> None:
        """把 os.walk 本身失败的目录转换为显式相对路径错误条目。"""
        filename = Path(getattr(exc, "filename", None) or root)
        try:
            relative_path = filename.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            relative_path = ""
        errors.append({"relative_path": relative_path, "error": str(exc)})

    for current, directory_names, file_names in os.walk(root, followlinks=False, onerror=on_walk_error):
        try:
            _check_deadline(deadline, _relative_path(root, Path(current)))
        except TimeoutError as exc:
            errors.append({"relative_path": "", "error": str(exc)})
            break

        kept_directories: list[str] = []
        for name in directory_names:
            path = Path(current) / name
            relative_path = _relative_path(root, path)
            try:
                mode = path.lstat().st_mode
                if not stat.S_ISDIR(mode):
                    continue
                if is_excluded(relative_path, excludes):
                    continue
                kept_directories.append(name)
            except (OSError, ValueError) as exc:
                errors.append({"relative_path": relative_path, "error": str(exc)})
        directory_names[:] = kept_directories

        for name in file_names:
            path = Path(current) / name
            relative_path = _relative_path(root, path)
            try:
                _check_deadline(deadline, relative_path)
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode):
                    continue
                if is_excluded(relative_path, excludes):
                    continue
                entries.append(_scan_file(path, relative_path, deadline))
            except TimeoutError as exc:
                errors.append({"relative_path": relative_path, "error": str(exc)})
                return sorted(entries, key=lambda entry: entry.relative_path), errors
            except (OSError, ValueError) as exc:
                errors.append({"relative_path": relative_path, "error": str(exc)})

    return sorted(entries, key=lambda entry: entry.relative_path), errors
