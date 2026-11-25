"""单预览令牌、互斥领取、心跳和原子状态持久化。"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Callable

from tools.sync.models import LocalFileEntry, SyncPlan, SyncPlanItem


class TokenStateError(Exception):
    """表示令牌已过期、已使用、忙碌或校验不匹配。"""


def local_entry_to_dict(entry: LocalFileEntry) -> dict:
    """把本地文件条目转换为可 JSON 持久化的字典。"""
    return {
        "relative_path": entry.relative_path,
        "absolute_path": str(entry.absolute_path),
        "raw_size": entry.raw_size,
        "raw_sha256": entry.raw_sha256,
        "upload_size": entry.upload_size,
        "upload_sha256": entry.upload_sha256,
        "line_ending_converted": entry.line_ending_converted,
        "final_mode": entry.final_mode,
        "mode_source": entry.mode_source,
    }


def item_to_dict(item: SyncPlanItem) -> dict:
    """把计划条目转换为详情分页和传输需要的字典。"""
    return {
        "relative_path": item.relative_path,
        "kind": item.kind,
        "local": local_entry_to_dict(item.local) if item.local is not None else None,
        "remote_size": item.remote_size,
        "remote_sha256": item.remote_sha256,
        "final_mode": item.final_mode,
        "mode_source": item.mode_source,
        "blocked_reason": item.blocked_reason,
    }


def _plan_state(plan: SyncPlan, token: str, config_fingerprint: str, now: float, expires_at: float) -> dict:
    """把计划转换为唯一预览令牌的完整持久化状态。"""
    return {
        "token": token,
        "state": "ready",
        "created_at": now,
        "expires_at": expires_at,
        "config_fingerprint": config_fingerprint,
        "confirm_digest": plan.confirm_digest,
        "root_realpath": plan.root_realpath,
        "window": [item_to_dict(item) for item in plan.window],
        "deferred": [item_to_dict(item) for item in plan.deferred],
        "items": {path: item_to_dict(item) for path, item in plan.items.items()},
        "summary": dict(plan.summary),
        "plan_status": plan.status,
        "has_more": plan.has_more,
        "deferred_count": len(plan.deferred),
        "conflicts": list(plan.conflicts),
        "errors": list(plan.errors),
        "remote_excluded_count": plan.remote_excluded_count,
        "owner_pid": None,
        "started_at": None,
        "progress_at": None,
        "result": None,
    }


class TokenStore:
    """管理唯一有效预览令牌，并把所有状态变更原子落盘。"""

    def __init__(
        self,
        state_dir: Path,
        token_ttl_seconds: int = 900,
        transfer_timeout_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.current_path = self.state_dir / "current.json"
        self.lock_path = self.state_dir / "claim.lock"
        self.token_ttl_seconds = token_ttl_seconds
        self.transfer_timeout_seconds = transfer_timeout_seconds
        self.clock = clock
        self.stale_after = max(token_ttl_seconds, 3 * transfer_timeout_seconds)

    def _now(self) -> float:
        """返回注入的持久化时钟值。"""
        return float(self.clock())

    def _read_state(self) -> dict | None:
        """读取状态；文件不存在返回 None，损坏或 schema 非法返回 corrupted。"""
        try:
            with self.current_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, json.JSONDecodeError):
            return {"state": "corrupted", "status": "state_corrupted"}
        if not self._valid_state(state):
            return {"state": "corrupted", "status": "state_corrupted"}
        state.setdefault("deferred", [])
        return state

    @staticmethod
    def _valid_state(state) -> bool:
        """校验状态必需字段和基本类型，防止合法 JSON 半损坏穿透。"""
        if not isinstance(state, dict):
            return False
        required = {
            "token",
            "state",
            "created_at",
            "expires_at",
            "config_fingerprint",
            "confirm_digest",
            "root_realpath",
            "window",
            "items",
            "summary",
            "plan_status",
            "has_more",
            "deferred_count",
            "conflicts",
            "errors",
            "remote_excluded_count",
            "owner_pid",
            "started_at",
            "progress_at",
            "result",
        }
        if not required.issubset(state):
            return False
        return (
            isinstance(state["token"], str)
            and isinstance(state["state"], str)
            and isinstance(state["created_at"], (int, float))
            and isinstance(state["expires_at"], (int, float))
            and isinstance(state["config_fingerprint"], str)
            and isinstance(state["confirm_digest"], str)
            and isinstance(state["root_realpath"], str)
            and isinstance(state["window"], list)
            and isinstance(state.get("deferred", []), list)
            and isinstance(state["items"], dict)
            and isinstance(state["summary"], dict)
            and isinstance(state["plan_status"], str)
            and isinstance(state["has_more"], bool)
            and isinstance(state["deferred_count"], int)
            and isinstance(state["conflicts"], list)
            and isinstance(state["errors"], list)
            and isinstance(state["remote_excluded_count"], int)
            and (state["owner_pid"] is None or isinstance(state["owner_pid"], int))
            and (state["started_at"] is None or isinstance(state["started_at"], (int, float)))
            and (state["progress_at"] is None or isinstance(state["progress_at"], (int, float)))
            and (state["result"] is None or isinstance(state["result"], dict))
        )

    def _atomic_write(self, data: dict) -> None:
        """通过同目录临时文件、fsync 和 os.replace 原子写入状态。"""
        temp_path = self.state_dir / f".current.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.current_path)
            if os.name != "nt":
                directory_fd = os.open(self.state_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _acquire_lock(self, token: str) -> None:
        """用 O_CREAT|O_EXCL 获取领取锁，并回收超时锁。"""
        for _ in range(2):
            try:
                descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        json.dump(
                            {"pid": os.getpid(), "token": token, "acquired_at": self._now()},
                            handle,
                            ensure_ascii=False,
                        )
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    raise
                return
            except FileExistsError:
                if self._lock_is_stale():
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise TokenStateError("busy")
        raise TokenStateError("busy")

    def _lock_is_stale(self) -> bool:
        """判断现有锁是否超过令牌 TTL。"""
        try:
            with self.lock_path.open("r", encoding="utf-8") as handle:
                lock = json.load(handle)
            acquired_at = float(lock.get("acquired_at", 0))
            return self._now() - acquired_at >= self.token_ttl_seconds
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            try:
                return self._now() - self.lock_path.stat().st_mtime >= self.token_ttl_seconds
            except OSError:
                return False

    def _release_lock(self) -> None:
        """释放本次领取锁。"""
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _load_current(self, token: str, allow_stale: bool = False) -> dict:
        """读取并校验当前令牌，必须处于 ready 或 applying 状态。"""
        state = self._read_state()
        if state is None:
            raise TokenStateError("stale")
        if state.get("state") == "corrupted":
            raise TokenStateError("state_corrupted")
        if state.get("token") != token:
            raise TokenStateError("stale")
        if state.get("state") not in ("ready", "applying"):
            raise TokenStateError("stale")
        if allow_stale:
            return state
        now = self._now()
        if state["state"] == "ready" and now >= float(state["expires_at"]):
            raise TokenStateError("expired")
        if state["state"] == "applying":
            progress_at = float(state.get("progress_at") or state.get("started_at") or 0)
            if now - progress_at >= self.stale_after:
                raise TokenStateError("expired")
        return state

    def load(self, token: str) -> dict:
        """返回当前令牌状态；损坏状态返回结构化结果而不是 JSON 异常。"""
        state = self._read_state()
        if state is not None and state.get("state") == "corrupted":
            return state
        return self._load_current(token)

    def create(self, plan: SyncPlan, config_fingerprint: str) -> str:
        """创建唯一预览令牌；活跃 applying 状态拒绝覆盖。"""
        self._acquire_lock("create")
        try:
            now = self._now()
            current = self._read_state()
            if current is not None and current.get("state") == "applying":
                progress_at = float(current.get("progress_at") or current.get("started_at") or 0)
                if now - progress_at < self.stale_after:
                    raise TokenStateError("applying")
            token = secrets.token_urlsafe(32)
            state = _plan_state(
                plan,
                token,
                config_fingerprint,
                now,
                now + self.token_ttl_seconds,
            )
            self._atomic_write(state)
            return token
        finally:
            self._release_lock()

    def claim(self, token: str, confirm_digest: str, config_fingerprint: str) -> dict:
        """互斥领取 ready 令牌，并先持久化 applying 再返回。"""
        self._acquire_lock(token)
        try:
            state = self._load_current(token)
            if state["state"] != "ready":
                raise TokenStateError("stale")
            if state.get("confirm_digest") != confirm_digest:
                raise TokenStateError("digest")
            if state.get("config_fingerprint") != config_fingerprint:
                raise TokenStateError("config")
            now = self._now()
            state["state"] = "applying"
            state["owner_pid"] = os.getpid()
            state["started_at"] = now
            state["progress_at"] = now
            self._atomic_write(state)
            return state
        finally:
            self._release_lock()

    def touch(self, token: str) -> None:
        """仅在 applying 状态刷新进度心跳；遇到并发领取时静默跳过。"""
        try:
            self._acquire_lock(token)
        except TokenStateError:
            return
        try:
            state = self._read_state()
            if state is None or state.get("state") != "applying" or state.get("token") != token:
                return
            state["progress_at"] = self._now()
            self._atomic_write(state)
        finally:
            self._release_lock()

    def finish(self, token: str, result: dict) -> None:
        """写入终态，成功、失败和部分成功都消费原令牌。"""
        self._acquire_lock(token)
        try:
            state = self._load_current(token)
            if state["state"] != "applying":
                raise TokenStateError("stale")
            state["state"] = "consumed"
            state["result"] = result
            state["progress_at"] = self._now()
            self._atomic_write(state)
        finally:
            self._release_lock()

    def cancel(self, token: str, force: bool = False) -> None:
        """取消 ready 令牌；applying 默认拒绝，force 可清除失联状态。"""
        self._acquire_lock(token)
        try:
            state = self._load_current(token, allow_stale=force)
            if state["state"] == "applying" and not force:
                raise TokenStateError("applying")
            state["state"] = "cancelled"
            state["progress_at"] = self._now()
            self._atomic_write(state)
        finally:
            self._release_lock()
