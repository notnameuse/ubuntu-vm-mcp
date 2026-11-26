# ubuntu-vm-mcp

## 是什么
把 Windows 本地的代码改动同步到 Ubuntu 虚拟机，并在虚拟机里完成 CMake 配置、编译与测试的 MCP 服务。
可接入任何支持 stdio MCP 的 Agent。整体流程四步：装依赖 → 部署密钥 → 写配置 → 贴 mcp 片段。

## 解决什么问题
如果用AI Agent辅助开发Linux项目时，由于开发环境基本在虚拟机的Ubuntu系统里，基本上我都是在本地的windows系统上用AI改好代码，然后再上传到Ubuntu端
进行测试，但这样太麻烦了，上传需要花时间；并且上传之后还不知道能不能编译过，编译不过又要回来告诉本地上的AI Agent，就这样一直来回做ubuntu端和本地端AI的传声筒
，太麻烦了；之前也想过在ubuntu系统里装个AI Agent/IDE不就好了嘛？但是有的开发环境用的是Ubuntu老版本，对这些AI Agent/IDE的兼容性并不好，非常不方便。因此，又想兼容性好
，又不想来回做传声筒，我就想开发一个MCP，用于连接AI和ubuntu，当AI开发完代码之后，自动同步改动代码到ubuntu端旧仓库，并自动完成编译/测试，如果编译/测试不通过，那么AI自己
就可以发现并修改，不需要自己幸苦去做传话筒了。


## 快速开始

### 1. 装依赖

```bash
# 在本地终端执行即可，无需去ubuntu端
git clone https://github.com/notnameuse/ubuntu-vm-mcp.git
cd ubuntu-vm-mcp
python -m pip install -r requirements.txt
```

要求本地 Python 3.11+（实测 3.11.0）；远端 Ubuntu 需已运行 `openssh-server`，并装好 `cmake` 与项目工具链。

### 2. 部署 SSH 密钥

在**本地**终端执行（会提示输入一次 VM 密码）：

```bash
# 在你的本地终端运行即可，记得填你的Ubuntu端IP/还有你的用户名
# 注意：执行前一定要开启你的虚拟机，否则运行报错，其他故障排查见【故障排查】章节
python scripts/deploy_key.py --host <VM_IP> --user <VM_USER>
```

成功会打印 `Host key pinned: SHA256:<43位base64>` 和 `Private key: <路径>`，后者要填进下一步的 `ssh_key_path`。
NAT 端口转发时加 `--port 2222`。报 `host key mismatch`、known_hosts 解析失败、想换密钥位置 → 见「故障排查」。

### 3. 写配置

把仓库里的 `config.example.json` **复制或重命名为 `config.json`**，然后用文本编辑器打开，把表里这些字段填掉：

| 字段 | 说明 |
|---|---|
| `ssh_host` / `ssh_port` / `ssh_user` | VM 地址、端口（默认 22）、登录用户 |
| `ssh_key_path` | 上一步生成的私钥**绝对路径** |
| `project_root` | 远端项目根，POSIX 绝对路径，**必须已存在且是真实目录** |
| `local_project_root` | 本地项目根**绝对路径**，不能是盘符根或用户主目录 |
| `verify_host_keys` | 保持 `true` |
| `build_dir` | 远端构建目录名，默认 `build` |
| `command_timeout_seconds` | 单条远端命令超时，默认 120 秒；大型工程 configure/全量构建要调大 |
| `sync_excludes` | 追加排除规则（相对路径） |
| `sync_state_path` / `sync_token_ttl_seconds` / `sync_scan_timeout_seconds` / `sync_transfer_timeout_seconds` / `sync_max_files_per_call` / `log_max_bytes` | 令牌与预算类参数，一般不用改 |
| `verify_targets` / `verify_ctest_filter` / `verify_direct_tests` | `vm_verify_stage6` 的目标与过滤，**按你的项目改** |

**所有 `<...>` 占位符都要替换**，Windows 路径写正斜杠（`C:/Users/you/...`）。`config.json` 已被 `.gitignore` 忽略，不会进版本库。

### 4. 接入 Agent

把下面这段放进 Agent 的 MCP 配置，两条绝对路径改成你自己的：

```json
{
  "mcpServers": {
    "ubuntu-vm": {
      "type": "stdio",
      "command": "C:/path/to/python.exe",
      "args": ["C:/path/to/ubuntu-vm-mcp/server.py"]
    }
  }
}
```

| Agent | 配置文件 | 顶层键 |
|---|---|---|
| Qoder | 项目下 `.qoder/mcp.json` | `mcpServers` |
| Claude Desktop | `%APPDATA%/Claude/claude_desktop_config.json` | `mcpServers` |
| Cursor | `~/.cursor/mcp.json` | `mcpServers` |
| Cline | `%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | `mcpServers` |
| VS Code | 项目下 `.vscode/mcp.json` | **`servers`**（不是 `mcpServers`） |


改完**必须重载 MCP 或重启 Agent**：stdio 服务的工具清单在进程启动时固定，不重载就还跑旧代码。

## 使用

### 同步

在 Agent 里说「同步代码到 ubuntu」，标准流程：`vm_sync_preview` 扫描并签发令牌 → `vm_sync_detail` 看分类明细 → 你批准后 `vm_sync_apply` 带 `token` + `confirm_digest` 执行 → `vm_sync_status` 复核。

- **单向**：只上传本地存在的文件，远端多出来的归为 `remote_only`，只列出、不删除
- **哈希校验**：先写 `.vm_sync-<32hex>.tmp` 再 rename，落地后比对 SHA256
- **`.sh` 特殊处理**：上传前 CRLF 转 LF，权限 `0755`；其他文件原样，新文件 `0644`
- **令牌绑定**：有效期 900 秒，绑定配置指纹与计划摘要；文件或配置一变，apply 返回 `rejected_stale` / `rejected_config_changed`
- **分批**：一次最多 100 个文件，超出返回 `batch_applied` + `has_more`
- **无单文件参数**：apply 按整个窗口执行；只想传个别文件就用 `sync_excludes` 收窄范围，或自己 scp

### 编译

`vm_cmake_configure` → `vm_build` → `vm_run_ctest`，需要一次性回归用 `vm_verify_stage6`。

`vm_build` 默认**不传 `-j`（串行）**，多核请显式传 `jobs`，例如 `vm_build(jobs=8)`。

## 参考

### 工具清单（13 个）

| 工具 | 作用 | 是否写远端 |
|---|---|---|
| `vm_check_connection` | 检查 SSH 连通性，返回主机名与内核版本 | 否 |
| `vm_sync_status` | 扫描差异，返回摘要与本批窗口条目，不签发令牌 | 否 |
| `vm_sync_preview` | 扫描并签发一次性 `token` 与 `confirm_digest` | 否 |
| `vm_sync_detail` | 按分类分页查看差异明细 | 否 |
| `vm_sync_apply` | 按预览窗口上传并校验哈希 | **是** |
| `vm_sync_cancel` | 作废当前预览令牌 | 否 |
| `vm_cmake_configure` | 在远端执行 cmake 配置 | 是（构建目录） |
| `vm_build` | 在远端执行 cmake --build | 是（构建目录） |
| `vm_run_ctest` | 在远端执行 ctest，额外返回 `exit_code` | 是（构建目录：`Testing/Temporary/` 日志） |
| `vm_verify_stage6` | 构建指定目标 + 直跑测试 + ctest 回归 | 是（构建目录） |
| `vm_collect_log` | 读取远端项目内日志（按 `log_max_bytes` 截断） | 否 |
| `vm_exec` | 在项目根执行任意远端命令 | 取决于命令 |
| `vm_run_project_command` | 在指定工作目录执行远端命令 | 取决于命令 |

`vm_exec` 的 stdout 超过 10 KB 会截断并附 `[truncated]`；`vm_collect_log` 是读完再截，大文件仍会全量传输。

默认排除（内置不可关闭）：`.git`、`.qoder`、`.vscode`、`project_doc`、`build`、`out`、`vision_deploy`、`__pycache__`、`.pytest_cache`、`.venv`、`venv`、`compile_commands.json`、`test.db`、`cmake-build-*`、`docs/superpowers`、`*.o`、`*.a`、`*.so`、`*.exe`。

### 故障排查

| 现象 | 原因与处理 |
|---|---|
| `connected: false` | VM 未开机 / IP 或端口错 / `openssh-server` 没运行。先在本地 `ssh <user>@<host>` 验证 |
| `[Errno 11001] getaddrinfo failed` / `Name or service not known` | `ssh_host` 占位符没替换或地址不可达（异常类型是 `socket.gaierror`，但输出里没有这个词） |
| `local project root must be absolute` / `must not be a filesystem or user root` | `local_project_root` 没替换成绝对路径，或指到了盘根/用户主目录 |
| `Invalid \escape: line 1 column ...` | `config.json` 里 Windows 路径用了单反斜杠，改正斜杠或双反斜杠（经同步配置加载时报 `invalid sync config: Invalid \escape: ...`） |
| `invalid sync config: ...` | `config.json` 字段缺失或类型错；另注意远端 `project_root` 必须预先存在，否则 preview/status 直接 `failed` |
| `host key mismatch`（退出码 1） | VM 重装或指纹变了。**这是保护不是故障**。`ssh-keygen -R <VM_IP>` 删旧条目后重跑；非 22 端口必须写 `ssh-keygen -R "[<VM_IP>]:2222"` |
| 密钥脚本报 `Incorrect padding` / 加载 known_hosts 失败 | known_hosts 含 `@cert-authority`、`@revoked` 或损坏行，paramiko 解析不了。人工清理后重试 |
| `Permission denied (publickey)` | 公钥没进 `authorized_keys`，或 `ssh_key_path` 指错。重跑密钥脚本 |
| 指纹要不要信 | 脚本是 TOFU：**指纹打印时条目已写入**。与 VM 上 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` 的 SHA256 对照，不一致就用上面的 `-R` 删掉重来 |
| 指纹重复追加 / mismatch 不触发 | 你的 known_hosts 开了 `HashKnownHosts`（条目形如 `\|1\|<salt>\|<hash>`），脚本匹配不到明文条目。用 `ssh-keygen -F <host>` 自行核对 |
| 命令超时（`exit_code=124`，`command timed out after 120 seconds`） | 调大 `command_timeout_seconds`；远端命令仍在继续跑，并没有被中止 |
| 工具调用超时但远端文件已写入 | 旧版本 PTY 缺陷（已修复）：升级到本版本并重载 MCP |
| 需要 tty 的远端交互脚本异常 | 已知限制：`vm_exec` 不分配 PTY（为了正确送达 EOF），需要 tty 的命令自行 `ssh -t` |
| Agent 里看不到 `vm_sync_*` 工具 | MCP 进程是改动前启动的，重载 MCP 或重启 Agent |
| 第三方项目首次 `vm_verify_stage6` 构建失败 | 预期行为：`verify_targets` 默认是本项目目标，改成你自己的；空数组/空串不代表关闭，会回退默认并打 `为空，已回退默认值` |
| 编译报找不到编译器 | 远端工具链未装或 `build_dir` 里残留旧 CMakeCache，删掉远端构建目录后重新 configure |

### 安全说明

- 私钥只在本地生成，绝不上传；密码仅用于一次公钥部署，不落盘、不写日志、不打印
- `verify_host_keys` 默认 `true`，未知主机直接拒连
- 同步层的路径校验拒绝符号链接与 `..` 逃逸，只写项目根内的普通文件；密钥脚本在 `~/.ssh` 下用 `shlex.quote` 幂等追加，不适用该校验
- `vm_exec` / `vm_run_project_command` **不做命令白名单或黑名单**，等价于以 `ssh_user` 身份执行任意 shell；要隔离请自备受限账号、容器或快照
- `vm_run_project_command` 的 `working_directory` 允许绝对路径，可以在项目根之外执行；只有同步层才有目录逃逸校验
- 错误消息会移除私钥路径；配置类型写错时只打键名与类型名，不打配置值

### 开发

```bash
python -m pytest -q
```

当前基线：244 passed, 3 skipped。

仓库通过 `.gitattributes` 强制 `* text=auto eol=lf`，即使 `core.autocrlf=true` 新克隆也是 LF；请勿删除，否则协作时会出现整文件 CRLF 差异。

### 许可证

MIT，见 `LICENSE`。
