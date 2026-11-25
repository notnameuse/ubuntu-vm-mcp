from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import anyio

from ssh_client import SSHClient
from tools import sync_tools
from tools.sync.result import make_result
from tools.cmake_tools import vm_cmake_configure, vm_build
from tools.ctest_tools import vm_run_ctest
from tools.log_tools import vm_collect_log
from tools.verify_tools import vm_verify_stage6
from tools.exec_tools import vm_exec, vm_run_project_command

mcp = FastMCP("ubuntu-vm")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
LOCAL_STATE_CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def with_ssh(action):
    with SSHClient() as ssh:
        return action(ssh)


def run_sync_action(operation, action):
    """捕获同步工具入口异常并转换为结构化失败结果。"""
    try:
        return action()
    except Exception as exc:
        return make_result(False, "failed", operation, error=sync_tools.safe_error(exc))


@mcp.tool(name="vm_check_connection")
def vm_check_connection_tool() -> dict:
    try:
        def run_check(ssh):
            out, err, code = ssh.exec("hostname && uname -r")
            if code != 0:
                return {"connected": False, "error": err.strip()}
            lines = out.strip().splitlines()
            return {
                "connected": True,
                "hostname": lines[0] if len(lines) > 0 else "",
                "kernel": lines[1] if len(lines) > 1 else "",
            }

        return with_ssh(run_check)
    except Exception as e:
        return {"connected": False, "error": str(e)}


@mcp.tool(name="vm_cmake_configure")
def vm_cmake_configure_tool(extra_args: str = "") -> dict:
    return with_ssh(lambda ssh: vm_cmake_configure(ssh, extra_args=extra_args))


@mcp.tool(name="vm_build")
def vm_build_tool(targets: list[str] | None = None, jobs: int | None = None) -> dict:
    return with_ssh(lambda ssh: vm_build(ssh, targets=targets, jobs=jobs))


@mcp.tool(name="vm_run_ctest")
def vm_run_ctest_tool(filter: str | None = None, verbose: bool = False) -> dict:
    return with_ssh(lambda ssh: vm_run_ctest(ssh, filter=filter, verbose=verbose))


@mcp.tool(name="vm_collect_log")
def vm_collect_log_tool(file_path: str) -> dict:
    return with_ssh(lambda ssh: vm_collect_log(ssh, file_path=file_path))


@mcp.tool(name="vm_verify_stage6")
def vm_verify_stage6_tool() -> dict:
    return with_ssh(vm_verify_stage6)


@mcp.tool(name="vm_exec")
def vm_exec_tool(command: str, stdin_text: str | None = None) -> dict:
    if stdin_text is None:
        return with_ssh(lambda ssh: vm_exec(ssh, command=command))
    return with_ssh(lambda ssh: vm_exec(ssh, command=command, stdin_text=stdin_text))


@mcp.tool(name="vm_run_project_command")
def vm_run_project_command_tool(
    command: str,
    working_directory: str = ".",
    stdin_text: str | None = None,
) -> dict:
    if stdin_text is None:
        return with_ssh(
            lambda ssh: vm_run_project_command(
                ssh,
                command=command,
                working_directory=working_directory,
            )
        )
    return with_ssh(
        lambda ssh: vm_run_project_command(
            ssh,
            command=command,
            working_directory=working_directory,
            stdin_text=stdin_text,
        )
    )


@mcp.tool(name="vm_sync_preview", annotations=READ_ONLY)
async def vm_sync_preview_tool() -> dict:
    return await anyio.to_thread.run_sync(
        lambda: run_sync_action("preview", lambda: with_ssh(sync_tools.vm_sync_preview))
    )


@mcp.tool(name="vm_sync_detail", annotations=READ_ONLY)
async def vm_sync_detail_tool(token: str, kind: str, offset: int = 0, limit: int = 100) -> dict:
    return run_sync_action("detail", lambda: sync_tools.vm_sync_detail(token, kind, offset, limit))


@mcp.tool(name="vm_sync_status", annotations=READ_ONLY)
async def vm_sync_status_tool() -> dict:
    return await anyio.to_thread.run_sync(
        lambda: run_sync_action("status", lambda: with_ssh(sync_tools.vm_sync_status))
    )


@mcp.tool(
    name="vm_sync_apply",
    annotations=DESTRUCTIVE,
    description="必须先展示 preview/detail 并获得用户明确批准，禁止自动串联执行；确认摘要仅绑定计划内容。",
)
async def vm_sync_apply_tool(token: str, confirm_digest: str) -> dict:
    return await anyio.to_thread.run_sync(
        lambda: run_sync_action(
            "apply",
            lambda: with_ssh(lambda ssh: sync_tools.vm_sync_apply(ssh, token, confirm_digest)),
        )
    )


@mcp.tool(name="vm_sync_cancel", annotations=LOCAL_STATE_CHANGE)
async def vm_sync_cancel_tool(token: str, force: bool = False) -> dict:
    return run_sync_action("cancel", lambda: sync_tools.vm_sync_cancel(token, force))


if __name__ == "__main__":
    mcp.run()
