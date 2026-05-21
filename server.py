from mcp.server.fastmcp import FastMCP

from ssh_client import SSHClient
from tools.cmake_tools import vm_cmake_configure, vm_build
from tools.ctest_tools import vm_run_ctest
from tools.log_tools import vm_collect_log
from tools.verify_tools import vm_verify_stage6
from tools.exec_tools import vm_exec

mcp = FastMCP("ubuntu-vm")
ssh = SSHClient()


@mcp.tool(name="vm_check_connection")
def vm_check_connection_tool() -> dict:
    try:
        out, err, code = ssh.exec("hostname && uname -r")
        if code != 0:
            return {"connected": False, "error": err.strip()}
        lines = out.strip().splitlines()
        return {
            "connected": True,
            "hostname": lines[0] if len(lines) > 0 else "",
            "kernel": lines[1] if len(lines) > 1 else "",
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


@mcp.tool(name="vm_cmake_configure")
def vm_cmake_configure_tool(extra_args: str = "") -> dict:
    return vm_cmake_configure(ssh, extra_args=extra_args)


@mcp.tool(name="vm_build")
def vm_build_tool(targets: list[str] | None = None, jobs: int = 4) -> dict:
    return vm_build(ssh, targets=targets, jobs=jobs)


@mcp.tool(name="vm_run_ctest")
def vm_run_ctest_tool(filter: str | None = None, verbose: bool = False) -> dict:
    return vm_run_ctest(ssh, filter=filter, verbose=verbose)


@mcp.tool(name="vm_collect_log")
def vm_collect_log_tool(file_path: str) -> dict:
    return vm_collect_log(ssh, file_path=file_path)


@mcp.tool(name="vm_verify_stage6")
def vm_verify_stage6_tool() -> dict:
    return vm_verify_stage6(ssh)


@mcp.tool(name="vm_exec")
def vm_exec_tool(command: str) -> dict:
    return vm_exec(ssh, command=command)


if __name__ == "__main__":
    mcp.run()
