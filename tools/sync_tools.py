"""五个代码同步 MCP 工具的业务门面。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tools.sync.config import SyncConfig, config_fingerprint, load_sync_config
from tools.sync.models import LocalFileEntry, SyncPlan, SyncPlanItem
from tools.sync.plan import build_sync_plan
from tools.sync.remote_index import RemoteIndex, build_remote_index
from tools.sync.result import make_result, paginate_items
from tools.sync.scanner import scan_local
from tools.sync.token_store import TokenStateError, TokenStore, item_to_dict
from tools.sync.transfer import apply_window


DETAIL_KINDS = {"add", "overwrite", "unchanged", "remote_only", "failed", "deferred", "conflicts"}


def safe_error(exc: Exception, config: SyncConfig | None = None) -> str:
    """移除错误消息中的 SSH 私钥路径。"""
    message = str(exc)
    key_path = config.ssh_key_path if config is not None else None
    if not key_path:
        try:
            raw = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
            key_path = raw.get("ssh_key_path")
        except (OSError, ValueError, TypeError):
            key_path = None
    if key_path:
        message = message.replace(str(key_path), "<redacted>")
    return message


def _local_from_dict(data: dict) -> LocalFileEntry:
    """从令牌状态恢复本地文件条目。"""
    return LocalFileEntry(
        relative_path=data["relative_path"],
        absolute_path=Path(data["absolute_path"]),
        raw_size=data["raw_size"],
        raw_sha256=data["raw_sha256"],
        upload_size=data["upload_size"],
        upload_sha256=data["upload_sha256"],
        line_ending_converted=data["line_ending_converted"],
        final_mode=data["final_mode"],
        mode_source=data["mode_source"],
    )


def _item_from_dict(data: dict) -> SyncPlanItem:
    """从令牌状态恢复计划条目。"""
    return SyncPlanItem(
        relative_path=data["relative_path"],
        kind=data["kind"],
        local=_local_from_dict(data["local"]) if data.get("local") is not None else None,
        remote_size=data.get("remote_size"),
        remote_sha256=data.get("remote_sha256"),
        final_mode=data["final_mode"],
        mode_source=data["mode_source"],
        blocked_reason=data.get("blocked_reason"),
    )


def _plan_from_state(state: dict) -> SyncPlan:
    """从 applying 令牌状态恢复 apply_window 所需的计划。"""
    return SyncPlan(
        status=state["plan_status"],
        items={path: _item_from_dict(item) for path, item in state["items"].items()},
        window=[_item_from_dict(item) for item in state["window"]],
        deferred=[_item_from_dict(item) for item in state.get("deferred", [])],
        conflicts=list(state.get("conflicts", [])),
        summary=dict(state.get("summary", {})),
        confirm_digest=state["confirm_digest"],
        has_more=bool(state.get("has_more")),
        root_realpath=state.get("root_realpath", ""),
        errors=list(state.get("errors", [])),
        remote_excluded_count=int(state.get("remote_excluded_count", 0)),
    )


class SyncTools:
    """加载配置、扫描计划、令牌状态和传输逻辑的统一门面。"""

    def __init__(self, config_path: Path, package_root: Path):
        self.config_path = Path(config_path)
        self.package_root = Path(package_root)

    def _load_config(self) -> SyncConfig:
        """每次工具调用都重新加载配置，避免缓存导致指纹失效。"""
        return load_sync_config(self.config_path, self.package_root)

    def _store(self, config: SyncConfig) -> TokenStore:
        """按当前配置创建令牌存储。"""
        return TokenStore(
            config.state_dir,
            token_ttl_seconds=config.sync_token_ttl_seconds,
            transfer_timeout_seconds=config.sync_transfer_timeout_seconds,
        )

    def _scan(self, ssh, config: SyncConfig) -> tuple[SyncPlan, RemoteIndex]:
        """执行本地/远端扫描、分类和失败关闭规则。"""
        local_entries, local_errors = scan_local(config)
        if local_errors:
            remote_index = RemoteIndex()
        else:
            deadline = time.monotonic() + config.sync_scan_timeout_seconds
            remote_index = build_remote_index(ssh, config, local_entries, deadline)
        plan = build_sync_plan(local_entries, remote_index, config, local_errors=local_errors)
        return plan, remote_index

    @staticmethod
    def _context(config: SyncConfig) -> dict:
        """返回结果中共享的根目录和排除规则。"""
        return {
            "local_project_root": str(config.local_project_root),
            "project_root": config.project_root,
            "effective_excludes": list(config.effective_excludes),
        }

    def _plan_result(
        self,
        plan: SyncPlan,
        config: SyncConfig,
        operation: str,
        token: str | None = None,
        state: dict | None = None,
    ) -> dict:
        """把计划转换为预览/状态统一结果。"""
        success = plan.status in ("ready", "no_changes")
        next_step = (
            "Review detail with the token, then call apply with the returned confirm_digest."
            if plan.status == "ready"
            else "No synchronization is required."
            if plan.status == "no_changes"
            else "Fix the reported errors or conflicts, then preview again."
        )
        if plan.errors:
            error = plan.errors[0].get("error")
        elif plan.conflicts:
            error = f"{len(plan.conflicts)} path conflict(s) require manual resolution"
        else:
            error = None
        return make_result(
            success,
            plan.status,
            operation,
            token_state=state.get("state") if state is not None else None,
            summary=dict(plan.summary),
            items=[item_to_dict(item) for item in plan.window],
            has_more=plan.has_more,
            next_step=next_step,
            token=token,
            expires_at=state.get("expires_at") if state is not None else None,
            confirm_digest=plan.confirm_digest,
            root_realpath=plan.root_realpath or None,
            remote_excluded_count=plan.remote_excluded_count,
            error=error,
            **self._context(config),
        )

    def _token_error(self, operation: str, exc: TokenStateError, config: SyncConfig) -> dict:
        """把令牌异常映射为统一失败结果。"""
        reason = str(exc)
        if reason == "digest":
            status, token_state = "rejected_stale", "ready"
        elif reason == "config":
            status, token_state = "rejected_config_changed", "ready"
        elif reason == "expired":
            status, token_state = "rejected_stale", "expired"
        elif reason == "applying":
            status, token_state = "failed", "applying"
        elif reason == "busy":
            status, token_state = "failed", "ready"
        elif reason == "state_corrupted":
            status, token_state = "state_corrupted", "corrupted"
        else:
            status, token_state = "rejected_stale", "stale"
        return make_result(
            False,
            status,
            operation,
            token_state=token_state,
            error=safe_error(exc, config),
            next_step="Preview again with the current configuration.",
            **self._context(config),
        )

    def preview(self, ssh) -> dict:
        """扫描并签发单一预览令牌，不修改远端。"""
        try:
            config = self._load_config()
        except Exception as exc:
            return make_result(False, "failed", "preview", error=str(exc))
        try:
            plan, _ = self._scan(ssh, config)
            store = self._store(config)
            token = store.create(plan, config_fingerprint(config))
            state = store.load(token)
            return self._plan_result(plan, config, "preview", token, state)
        except TokenStateError as exc:
            return self._token_error("preview", exc, config)
        except Exception as exc:
            return make_result(
                False,
                "failed",
                "preview",
                error=safe_error(exc, config),
                next_step="Check SSH, SFTP and local file permissions, then preview again.",
                **self._context(config),
            )

    def status(self, ssh) -> dict:
        """重新扫描并返回差异统计，不签发或替换令牌。"""
        try:
            config = self._load_config()
        except Exception as exc:
            return make_result(False, "failed", "status", error=str(exc))
        try:
            plan, _ = self._scan(ssh, config)
            return self._plan_result(plan, config, "status")
        except Exception as exc:
            return make_result(
                False,
                "failed",
                "status",
                error=safe_error(exc, config),
                next_step="Check SSH, SFTP and local file permissions, then retry.",
                **self._context(config),
            )

    def detail(self, token: str, kind: str, offset: int = 0, limit: int = 100) -> dict:
        """只从当前令牌状态分页读取指定分类，不连接 SSH。"""
        try:
            config = self._load_config()
        except Exception as exc:
            return make_result(False, "failed", "detail", error=str(exc))
        store = self._store(config)
        try:
            state = store.load(token)
        except TokenStateError as exc:
            return self._token_error("detail", exc, config)
        if state.get("state") == "corrupted":
            return make_result(
                False,
                "state_corrupted",
                "detail",
                token_state="corrupted",
                error="state_corrupted",
                **self._context(config),
            )
        if kind not in DETAIL_KINDS:
            return make_result(
                False,
                "failed",
                "detail",
                token_state=state.get("state"),
                error=f"unknown detail category: {kind}",
                **self._context(config),
            )

        if kind == "unchanged":
            source: list[dict] = []
        elif kind == "conflicts":
            source = list(state.get("conflicts", []))
        elif kind == "deferred":
            source = list(state.get("deferred", []))
        else:
            source = [item for item in state["items"].values() if item.get("kind") == kind]
        try:
            page, next_offset = paginate_items(source, offset, limit)
        except ValueError as exc:
            return make_result(
                False,
                "failed",
                "detail",
                token_state=state.get("state"),
                error=str(exc),
                **self._context(config),
            )
        plan_status = state.get("plan_status", "ready")
        if state.get("errors"):
            detail_error = state["errors"][0].get("error")
        elif state.get("conflicts"):
            detail_error = f"{len(state['conflicts'])} path conflict(s) require manual resolution"
        else:
            detail_error = None
        return make_result(
            plan_status in ("ready", "no_changes"),
            plan_status,
            "detail",
            token_state=state.get("state"),
            summary=dict(state.get("summary", {})),
            items=page,
            error=detail_error,
            has_more=next_offset is not None,
            next_offset=next_offset,
            next_step="Call detail again with next_offset." if next_offset is not None else None,
            **self._context(config),
        )

    def apply(self, ssh, token: str, confirm_digest: str) -> dict:
        """领取令牌并执行当前窗口的原子同步。"""
        try:
            config = self._load_config()
        except Exception as exc:
            return make_result(False, "failed", "apply", error=str(exc))
        store = self._store(config)
        try:
            state = store.load(token)
        except TokenStateError as exc:
            return self._token_error("apply", exc, config)
        if state.get("state") == "corrupted":
            return make_result(
                False,
                "state_corrupted",
                "apply",
                token_state="corrupted",
                error="state_corrupted",
                **self._context(config),
            )
        warnings = []
        if not config.verify_host_keys:
            warnings.append("host key verification is disabled")
        if state.get("plan_status") in ("failed", "blocked_conflict"):
            return make_result(
                False,
                state["plan_status"],
                "apply",
                token_state=state.get("state"),
                summary=dict(state.get("summary", {})),
                error="plan is not executable",
                warnings=warnings,
                next_step="Fix the reported errors and preview again.",
                **self._context(config),
            )
        if state.get("config_fingerprint") != config_fingerprint(config):
            return make_result(
                False,
                "rejected_config_changed",
                "apply",
                token_state=state.get("state"),
                error="configuration changed since preview",
                warnings=warnings,
                next_step="Preview again with the current configuration.",
                **self._context(config),
            )
        if state.get("confirm_digest") != confirm_digest:
            return make_result(
                False,
                "rejected_stale",
                "apply",
                token_state=state.get("state"),
                error="confirmation digest does not match the preview",
                warnings=warnings,
                next_step="Review the current preview and use its exact confirm_digest.",
                **self._context(config),
            )
        if not state.get("window") or state.get("plan_status") == "no_changes":
            try:
                store.cancel(token)
            except TokenStateError:
                pass
            return make_result(
                True,
                "no_changes",
                "apply",
                token_state="cancelled",
                summary=dict(state.get("summary", {})),
                warnings=warnings,
                next_step="No synchronization is required.",
                **self._context(config),
            )

        try:
            claimed = store.claim(token, confirm_digest, config_fingerprint(config))
        except TokenStateError as exc:
            return self._token_error("apply", exc, config)
        window_paths = [item.get("relative_path", "") for item in claimed.get("window", [])]
        result = make_result(
            False,
            "failed",
            "apply",
            token_state="applying",
            summary=dict(claimed.get("summary", {})),
            unprocessed=window_paths,
            error="internal synchronization failure",
            warnings=warnings,
            has_more=bool(claimed.get("has_more")),
            **self._context(config),
        )

        def heartbeat() -> None:
            """心跳失败只记警告，不能覆盖已完成的文件结果。"""
            try:
                store.touch(token)
            except Exception as exc:
                warnings.append(f"heartbeat write failed: {safe_error(exc, config)}")

        try:
            plan = _plan_from_state(claimed)
            deadline = time.monotonic() + max(
                config.sync_transfer_timeout_seconds * max(1, len(plan.window)),
                30,
            )
            transfer_result = apply_window(
                ssh,
                config,
                plan,
                deadline,
                progress=heartbeat,
            )
            status = transfer_result["status"]
            if status == "all_applied" and claimed.get("has_more"):
                status = "batch_applied"
            summary = dict(claimed.get("summary", {}))
            summary["unprocessed"] = len(transfer_result.get("unprocessed", []))
            summary["failed"] = len(transfer_result.get("failed", []))
            result = make_result(
                bool(transfer_result.get("success")),
                status,
                "apply",
                token_state="applying",
                summary=summary,
                applied=list(transfer_result.get("applied", [])),
                unprocessed=list(transfer_result.get("unprocessed", [])),
                failed=list(transfer_result.get("failed", [])),
                error=transfer_result.get("error"),
                warnings=warnings,
                has_more=bool(claimed.get("has_more")),
                next_step=(
                    "Preview again to synchronize the next batch."
                    if claimed.get("has_more")
                    else "Call status to verify the synchronized result."
                ),
                **self._context(config),
            )
        except Exception as exc:
            result["error"] = safe_error(exc, config)
        finally:
            try:
                store.finish(token, result)
                result["token_state"] = "consumed"
            except Exception as exc:
                result["warnings"].append(f"failed to persist terminal token state: {safe_error(exc, config)}")
                result["token_state"] = "applying"
        return result

    def cancel(self, token: str, force: bool = False) -> dict:
        """清除当前预览令牌，不连接 SSH。"""
        try:
            config = self._load_config()
        except Exception as exc:
            return make_result(False, "failed", "cancel", error=str(exc))
        store = self._store(config)
        try:
            store.cancel(token, force=force)
        except TokenStateError as exc:
            return self._token_error("cancel", exc, config)
        return make_result(
            True,
            "cancelled",
            "cancel",
            token_state="cancelled",
            next_step="Preview again when a new synchronization is needed.",
            **self._context(config),
        )


def vm_sync_preview(ssh, config_path=None) -> dict:
    """模块级预览封装。"""
    return SyncTools(Path(config_path or Path(__file__).parent.parent / "config.json"), Path(__file__).parent.parent).preview(ssh)


def vm_sync_status(ssh, config_path=None) -> dict:
    """模块级状态封装。"""
    return SyncTools(Path(config_path or Path(__file__).parent.parent / "config.json"), Path(__file__).parent.parent).status(ssh)


def vm_sync_apply(ssh, token, confirm_digest, config_path=None) -> dict:
    """模块级执行封装。"""
    return SyncTools(Path(config_path or Path(__file__).parent.parent / "config.json"), Path(__file__).parent.parent).apply(ssh, token, confirm_digest)


def vm_sync_detail(token, kind, offset=0, limit=100, config_path=None) -> dict:
    """模块级详情封装，不创建 SSH。"""
    return SyncTools(Path(config_path or Path(__file__).parent.parent / "config.json"), Path(__file__).parent.parent).detail(token, kind, offset, limit)


def vm_sync_cancel(token, force=False, config_path=None) -> dict:
    """模块级取消封装，不创建 SSH。"""
    return SyncTools(Path(config_path or Path(__file__).parent.parent / "config.json"), Path(__file__).parent.parent).cancel(token, force)
