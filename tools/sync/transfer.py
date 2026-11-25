"""两阶段 SFTP 传输、临时文件原子替换和上传后校验。"""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from tools.sync.config import MCP_SYNC_PREFIX, TEMP_FILE_PATTERN, SyncConfig
from tools.sync.models import SyncPlan, SyncPlanItem
from tools.sync.remote_index import SFTP_ERRORS, assert_safe_remote_path


CHUNK_SIZE = 1024 * 1024
TEMP_FILE_RE = re.compile(TEMP_FILE_PATTERN)


class _TransferAbort(Exception):
    """携带结构化状态和路径的传输中止信号。"""

    def __init__(self, status: str, message: str, path: str):
        super().__init__(message)
        self.status = status
        self.path = path


@dataclass
class _PreparedTransfer:
    """全局预检后保留的本地句柄和最终上传参数。"""

    item: SyncPlanItem
    path: Path
    handle: object
    shell_data: bytes | None
    upload_size: int
    upload_sha256: str
    final_mode: int


def _check_deadline(deadline: float, relative_path: str) -> None:
    """在本地/远端读取循环中检查截止时间。"""
    if time.monotonic() >= deadline:
        raise _TransferAbort("failed", f"transfer timed out while reading {relative_path}", relative_path)


def _hash_bytes_from_handle(handle, deadline: float, relative_path: str) -> tuple[int, str]:
    """从当前句柄位置分块计算大小和 SHA256。"""
    digest = hashlib.sha256()
    size = 0
    while True:
        _check_deadline(deadline, relative_path)
        chunk = handle.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _hash_remote_file(sftp, remote_path: str, deadline: float, relative_path: str) -> tuple[int, str]:
    """回读远端普通文件并返回大小和 SHA256。"""
    digest = hashlib.sha256()
    size = 0
    with sftp.open(remote_path, "rb") as handle:
        while True:
            _check_deadline(deadline, relative_path)
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _open_local_for_preflight(item: SyncPlanItem, deadline: float) -> _PreparedTransfer:
    """打开本地文件，校验 raw/upload 双哈希并保留句柄或转换后的字节。"""
    if item.local is None:
        raise _TransferAbort("failed", "window item has no local file", item.relative_path)
    path = item.local.absolute_path
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise _TransferAbort("rejected_stale", str(exc), item.relative_path) from exc
    if not stat.S_ISREG(mode):
        raise _TransferAbort("rejected_stale", "local file is not a regular file", item.relative_path)
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise _TransferAbort("rejected_stale", str(exc), item.relative_path) from exc

    try:
        size, sha256 = _hash_bytes_from_handle(handle, deadline, item.relative_path)
        is_shell = PurePosixPath(item.relative_path).suffix.lower() == ".sh"
        shell_data = None
        if is_shell:
            handle.seek(0)
            raw_data = handle.read()
            shell_data = raw_data.replace(b"\r\n", b"\n")
            upload_size = len(shell_data)
            upload_sha256 = hashlib.sha256(shell_data).hexdigest()
        else:
            upload_size = size
            upload_sha256 = sha256
        if size != item.local.raw_size or sha256 != item.local.raw_sha256:
            raise _TransferAbort("rejected_stale", "local raw hash changed", item.relative_path)
        if upload_size != item.local.upload_size or upload_sha256 != item.local.upload_sha256:
            raise _TransferAbort("rejected_stale", "local upload hash changed", item.relative_path)
        return _PreparedTransfer(
            item=item,
            path=path,
            handle=handle,
            shell_data=shell_data,
            upload_size=upload_size,
            upload_sha256=upload_sha256,
            final_mode=item.final_mode,
        )
    except Exception:
        handle.close()
        raise


def _verify_local_again(prepared: _PreparedTransfer, deadline: float) -> None:
    """正式上传前再次确认普通文件并从保留句柄校验原始哈希。"""
    try:
        mode = prepared.path.lstat().st_mode
    except OSError as exc:
        raise _TransferAbort("rejected_stale", str(exc), prepared.item.relative_path) from exc
    if not stat.S_ISREG(mode):
        raise _TransferAbort("rejected_stale", "local file changed type", prepared.item.relative_path)
    prepared.handle.seek(0)
    size, sha256 = _hash_bytes_from_handle(prepared.handle, deadline, prepared.item.relative_path)
    if size != prepared.item.local.raw_size or sha256 != prepared.item.local.raw_sha256:
        raise _TransferAbort("rejected_stale", "local file changed after preflight", prepared.item.relative_path)
    prepared.handle.seek(0)


def _target_path(root_realpath: str, relative_path: str) -> str:
    """拼接远端根和目标相对路径。"""
    return posixpath.join(root_realpath, *PurePosixPath(relative_path).parts)


def _parent_path(root_realpath: str, relative_path: str) -> str:
    """返回远端目标父目录的 POSIX 路径。"""
    parts = PurePosixPath(relative_path).parts
    return posixpath.join(root_realpath, *parts[:-1]) if len(parts) > 1 else root_realpath


def _assert_root(sftp, plan: SyncPlan) -> None:
    """确认远端根 realpath 与预览时一致。"""
    if not plan.root_realpath:
        raise _TransferAbort("blocked_conflict", "plan has no remote root realpath", "")
    normalized = sftp.normalize(plan.root_realpath)
    if normalized != plan.root_realpath:
        raise _TransferAbort("blocked_conflict", "remote root realpath changed", "")


def _check_remote_baseline(sftp, item: SyncPlanItem, plan: SyncPlan, deadline: float) -> None:
    """检查远端目标仍符合预览基线或新增目标仍不存在。"""
    target = assert_safe_remote_path(sftp, plan.root_realpath, item.relative_path)
    try:
        attr = sftp.lstat(target)
    except FileNotFoundError:
        if item.kind == "add":
            return
        raise _TransferAbort("rejected_stale", "remote target disappeared", item.relative_path)
    if item.kind == "add":
        raise _TransferAbort("rejected_stale", "new remote target already exists", item.relative_path)
    if not stat.S_ISREG(attr.st_mode):
        raise _TransferAbort("blocked_conflict", "remote target is not a regular file", item.relative_path)
    if item.remote_size is not None and int(attr.st_size) != item.remote_size:
        raise _TransferAbort("rejected_stale", "remote size changed", item.relative_path)
    if item.remote_sha256 is None:
        raise _TransferAbort("rejected_stale", "remote overwrite has no preview hash", item.relative_path)
    _, current_sha = _hash_remote_file(sftp, target, deadline, item.relative_path)
    if current_sha != item.remote_sha256:
        raise _TransferAbort("rejected_stale", "remote hash changed", item.relative_path)


def _ensure_parent_directories(sftp, plan: SyncPlan, relative_path: str) -> None:
    """按层级创建缺失父目录并显式设置 0755。"""
    parts = PurePosixPath(relative_path).parts[:-1]
    current = plan.root_realpath
    for part in parts:
        current = posixpath.join(current, part)
        try:
            attr = sftp.lstat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current, mode=0o755)
            except FileExistsError:
                attr = sftp.lstat(current)
                if stat.S_ISLNK(attr.st_mode) or not stat.S_ISDIR(attr.st_mode):
                    raise _TransferAbort("blocked_conflict", "remote parent changed type", relative_path)
                sftp.chmod(current, 0o755)
                continue
            sftp.chmod(current, 0o755)
            continue
        if not stat.S_ISDIR(attr.st_mode):
            raise _TransferAbort("blocked_conflict", "remote parent is not a directory", relative_path)


def _cleanup_stale_temps(sftp, parent_path: str, deadline: float) -> None:
    """只清理当前父目录中完整匹配固定命名的普通临时文件。"""
    try:
        attributes = sftp.listdir_attr(parent_path)
    except SFTP_ERRORS:
        return
    for attr in attributes:
        if TEMP_FILE_RE.fullmatch(attr.filename) and stat.S_ISREG(attr.st_mode):
            try:
                _check_deadline(deadline, attr.filename)
                sftp.remove(posixpath.join(parent_path, attr.filename))
            except SFTP_ERRORS:
                pass


def _remove_temp(sftp, temp_path: str) -> None:
    """失败时尽力删除本次临时文件。"""
    try:
        sftp.remove(temp_path)
    except SFTP_ERRORS:
        pass


def _failure_result(
    status: str,
    message: str,
    path: str,
    window: list[SyncPlanItem],
    applied: list[str],
) -> dict:
    """构造停止传输后的结构化结果。"""
    unprocessed = [item.relative_path for item in window if item.relative_path not in applied]
    return {
        "success": False,
        "status": "partial" if applied else status,
        "applied": applied,
        "unprocessed": unprocessed,
        "failed": [path] if path else [],
        "error": message,
    }


def apply_window(
    ssh,
    config: SyncConfig,
    plan: SyncPlan,
    deadline: float,
    progress: Callable[[], None] | None = None,
) -> dict:
    """预检整个窗口后逐文件原子上传，失败立即停止且不回滚。"""
    window = list(plan.window)
    if not window:
        return {
            "success": True,
            "status": "all_applied",
            "applied": [],
            "unprocessed": [],
            "failed": [],
            "error": None,
        }

    try:
        sftp = ssh.open_sftp(timeout_seconds=config.sync_transfer_timeout_seconds)
    except SFTP_ERRORS as exc:
        return _failure_result("failed", str(exc), "", window, [])
    prepared: list[_PreparedTransfer] = []
    applied: list[str] = []
    try:
        try:
            _assert_root(sftp, plan)
            for item in window:
                _check_deadline(deadline, item.relative_path)
                if progress is not None:
                    progress()
                _check_remote_baseline(sftp, item, plan, deadline)
                prepared.append(_open_local_for_preflight(item, deadline))
        except _TransferAbort as exc:
            return _failure_result(exc.status, str(exc), exc.path, window, [])
        except ValueError as exc:
            return _failure_result("blocked_conflict", str(exc), "", window, [])
        except SFTP_ERRORS as exc:
            return _failure_result("failed", str(exc), "", window, [])

        for prepared_item in prepared:
            item = prepared_item.item
            if progress is not None:
                progress()
            current_temp = None
            renamed = False
            try:
                file_deadline = min(deadline, time.monotonic() + config.sync_transfer_timeout_seconds)
                remaining_budget = max(0.1, file_deadline - time.monotonic())
                channel_timeout = max(0.1, min(float(config.sync_transfer_timeout_seconds), remaining_budget))
                sftp.get_channel().settimeout(channel_timeout)

                _assert_root(sftp, plan)
                _verify_local_again(prepared_item, file_deadline)
                _check_remote_baseline(sftp, item, plan, file_deadline)
                _ensure_parent_directories(sftp, plan, item.relative_path)
                parent_path = _parent_path(plan.root_realpath, item.relative_path)
                _cleanup_stale_temps(sftp, parent_path, file_deadline)

                temp_name = f".{MCP_SYNC_PREFIX}-{secrets.token_hex(16)}.tmp"
                temp_path = posixpath.join(parent_path, temp_name)
                current_temp = temp_path
                target_path = _target_path(plan.root_realpath, item.relative_path)
                source = io.BytesIO(prepared_item.shell_data) if prepared_item.shell_data is not None else prepared_item.handle
                sftp.putfo(
                    source,
                    temp_path,
                    file_size=prepared_item.upload_size,
                    confirm=True,
                    callback=lambda _sent, _total: _check_deadline(file_deadline, item.relative_path),
                )

                temp_size, temp_sha = _hash_remote_file(sftp, temp_path, file_deadline, item.relative_path)
                if temp_size != prepared_item.upload_size or temp_sha != prepared_item.upload_sha256:
                    raise _TransferAbort("failed", "temporary upload hash mismatch", item.relative_path)

                sftp.chmod(temp_path, prepared_item.final_mode)
                try:
                    sftp.posix_rename(temp_path, target_path)
                except SFTP_ERRORS as exc:
                    raise _TransferAbort("failed", f"atomic rename failed: {exc}", item.relative_path) from exc
                renamed = True

                final_size, final_sha = _hash_remote_file(sftp, target_path, file_deadline, item.relative_path)
                if final_size != prepared_item.upload_size or final_sha != prepared_item.upload_sha256:
                    raise _TransferAbort(
                        "failed",
                        "post-upload hash mismatch; target was replaced",
                        item.relative_path,
                    )
                applied.append(item.relative_path)
            except _TransferAbort as exc:
                if current_temp is not None and not renamed:
                    _remove_temp(sftp, current_temp)
                if progress is not None:
                    progress()
                return _failure_result(exc.status, str(exc), exc.path, window, applied)
            except ValueError as exc:
                if current_temp is not None and not renamed:
                    _remove_temp(sftp, current_temp)
                if progress is not None:
                    progress()
                return _failure_result("blocked_conflict", str(exc), item.relative_path, window, applied)
            except SFTP_ERRORS as exc:
                if current_temp is not None and not renamed:
                    _remove_temp(sftp, current_temp)
                if progress is not None:
                    progress()
                return _failure_result("failed", str(exc), item.relative_path, window, applied)
            if progress is not None:
                progress()

        return {
            "success": True,
            "status": "all_applied",
            "applied": applied,
            "unprocessed": [],
            "failed": [],
            "error": None,
        }
    finally:
        for item in prepared:
            item.handle.close()
