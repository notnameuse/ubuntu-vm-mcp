# ubuntu-vm-mcp

把 Windows 本地的代码改动同步到 Ubuntu 虚拟机，并在虚拟机里完成 CMake 配置、编译与测试的 MCP 服务。
适用于「本地 Windows 写代码 + 虚拟机 Ubuntu 编译验证」的开发方式，可接入任何支持 stdio MCP 的 Agent。

## 能力清单（13 个工具）

| 工具 | 作用 | 是否写远端 |
|---|---|---|
| `vm_check_connection` | 检查 SSH 连通性，返回主机名与内核版本 | 否 |
| `vm_sync_status` | 扫描本地与远端差异，返回摘要与本批窗口条目，但不签发令牌 | 否 |
| `vm_sync_preview` | 扫描并签发一次性 `token` 与 `confirm_digest`，返回本批窗口 | 否 |
| `vm_sync_detail` | 按分类分页查看差异明细 | 否 |
| `vm_sync_apply` | 按预览窗口上传文件并做 SHA256 校验 | **是** |
| `vm_sync_cancel` | 作废当前预览令牌 | 否 |
| `vm_cmake_configure` | 在远端执行 cmake 配置 | 是（构建目录） |
| `vm_build` | 在远端执行 cmake --build | 是（构建目录） |
| `vm_run_ctest` | 在远端执行 ctest | 是（构建目录：`Testing/Temporary/` 日志） |
| `vm_verify_stage6` | 构建指定目标 + 直跑测试 + ctest 回归 | 是（构建目录） |
| `vm_collect_log` | 读取远端项目内日志文件（截断保护） | 否 |
| `vm_exec` | 在项目根执行任意远端命令 | 取决于命令 |
| `vm_run_project_command` | 在指定工作目录执行远端命令 | 取决于命令 |

## 环境要求

- 本地：Windows / Linux / macOS，Python 3.11 或更高（实测 Python 3.11.0）
- 远端：Ubuntu 虚拟机，已安装并运行 `openssh-server`，已装 `cmake` 与项目所需工具链
- 网络可达：桥接或 NAT 端口转发均可，NAT 时把 `ssh_port` 改成转发端口并给脚本加 `--port`

## 安装

```bash
git clone <你的仓库地址> ubuntu-vm-mcp
cd ubuntu-vm-mcp
python -m pip install -r requirements.txt
```

## 第一步：部署 SSH 密钥

服务使用密钥登录，不走密码登录（密码只在 `deploy_key.py` 部署公钥那一步用一次）。注意服务端没有关闭本机默认密钥与 ssh-agent 查找，所以 `ssh_key_path` 写错时可能被本机其他密钥误打误撞地撑过去，排障时先确认这一把钥匙能直连。用自带脚本一次完成生密钥、固定主机指纹、部署公钥：

```bash
python scripts/deploy_key.py --host <VM_IP> --user <VM_USER>
```

- 默认密钥路径 `~/.ssh/ubuntu_vm_mcp_key`，可用 `--key-path` 改；已存在则复用，不会覆盖
- 主机指纹固定写入 `~/.ssh/known_hosts`，与服务端读取路径一致，不提供改路径的参数
- NAT 端口转发时加 `--port 2222`
- 过程中输入一次 VM 密码，密码只在内存中使用，不落盘、不写日志、不打印
- 成功输出 `Host key pinned: SHA256:<43位base64>` 与 `Private key: <路径>`

关于首次信任（TOFU）：**指纹打印时条目已经写入 known_hosts**，脚本不会先问你确认。请打印的指纹与 VM 上这条命令的输出对照：

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

如果不一致（例如 VM 被重装过），脚本报 `host key mismatch` 并以退出码 1 结束 —— **这是保护，不是故障**。先删掉旧条目再重跑即可，非标准端口必须带方括号：

```bash
ssh-keygen -R <VM_IP>
ssh-keygen -R "[<VM_IP>]:2222"
```

脚本是幂等的：重复运行不会重复追加公钥，也不会重复追加指纹行。但只适用于**明文条目**：若你的 known_hosts 开了 `HashKnownHosts`（条目形如 `|1|<salt>|<hash>`），脚本无法匹配到已有条目，会再追加一行明文；此时“拒绝改钉”的保护也不生效，请用 `ssh-keygen -F <host>` 自行核对。

## 第二步：写配置

```bash
cp config.example.json config.json
```

Windows PowerShell 用 `Copy-Item config.example.json config.json`。然后填这些字段：

| 字段 | 说明 |
|---|---|
| `ssh_host` | VM 地址，如 `192.168.1.50` |
| `ssh_port` | SSH 端口，默认 22 |
| `ssh_user` | VM 登录用户名 |
| `ssh_key_path` | 上一步生成的私钥**绝对路径** |
| `project_root` | 远端项目根，POSIX 绝对路径，如 `/home/user/linux/my_project` |
| `local_project_root` | 本地项目根**绝对路径** |
| `verify_host_keys` | 保持 `true`；主机指纹已由脚本固定 |
| `build_dir` | 远端构建目录名，默认 `build` |
| `sync_excludes` | 追加排除规则（相对路径），默认保护规则见下 |
| `verify_targets` / `verify_ctest_filter` / `verify_direct_tests` | `vm_verify_stage6` 用的目标与过滤，**按你的项目改** |
| `command_timeout_seconds` | 单条远端命令超时，默认 120 秒；大型工程 configure/全量构建必须调大 |
| `sync_state_path` | 预览令牌状态目录，必须落在仓库根内，默认 `sync_state` |
| `sync_token_ttl_seconds` / `sync_scan_timeout_seconds` / `sync_transfer_timeout_seconds` / `sync_max_files_per_call` | 令牌有效期、扫描预算、单文件传输预算、单批最大文件数 |
| `log_max_bytes` | `vm_collect_log` 返回内容的截断上限，默认 20480 |

**必须逐项替换掉所有 `<...>` 占位符。** 只替换一部分时的表现：`local_project_root` 没改会报 `local project root must be absolute`；`ssh_host` 没改会在连接阶段失败，工具返回的 `error` 文案是 `[Errno 11001] getaddrinfo failed`（Windows）或 `[Errno -2] Name or service not known`（Linux），**里面不会出现 `gaierror` 字样**（那只是异常类型名），看着像网络故障、其实是占位符没换。

另外两个容易踩的前提：
- **远端 `project_root` 必须已经存在且是真实目录**（本工具不会替你创建），否则 `vm_sync_preview` / `vm_sync_status` 直接返回 `failed`。
- `local_project_root` 不能是盘符根或用户主目录（会报 `local project root must not be a filesystem or user root`）。

**Windows 路径必须写正斜杠**：`ssh_key_path` 与 `local_project_root` 写成 `C:/Users/you/.ssh/ubuntu_vm_mcp_key`，或把反斜杠写成双反斜杠。直接粘 `C:\Users\...` 会让 JSON 解析失败（`\U` 是非法转义，报 `Invalid \escape`）。

`config.json` 已被 `.gitignore` 忽略，不会进版本库；仓库里公开的是 `config.example.json` 模板。

默认排除（内置、不可关闭）：`.git`、`.qoder`、`.vscode`、`project_doc`、`build`、`out`、`vision_deploy`、
`__pycache__`、`.pytest_cache`、`.venv`、`venv`、`compile_commands.json`、`test.db`、`cmake-build-*`、
`docs/superpowers`、`*.o`、`*.a`、`*.so`、`*.exe`。

**空值不等于关闭**：`verify_targets: []` 或 `verify_ctest_filter: ""` 会被判为无效配置，回退到本仓库自带的项目默认值，并在日志里给出 `为空，已回退默认值`。要真正跳过某个阶段，请把它配成你自己的合法值，而不是留空。

## 第三步：接入你的 Agent

把下面这段放进 Agent 的 MCP 配置，**两条绝对路径必须改成你自己的**：

> 如果你把配置放在项目内（Qoder 的 `.qoder/mcp.json`、VS Code 的 `.vscode/mcp.json`），**记得把 `.qoder/`、`.vscode/` 加进你自己的 `.gitignore`**，否则你的本地绝对路径和密钥路径会被提交到公开仓库。

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

各 Agent 的配置文件位置与顶层键名：

| Agent | 配置文件 | 顶层键 |
|---|---|---|
| Qoder | 项目下 `.qoder/mcp.json` | `mcpServers` |
| Claude Desktop | `%APPDATA%/Claude/claude_desktop_config.json` | `mcpServers` |
| Cursor | `~/.cursor/mcp.json` | `mcpServers` |
| Cline | `%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | `mcpServers` |
| VS Code | 项目下 `.vscode/mcp.json` | **`servers`**（不是 `mcpServers`） |

改完配置后**必须重载 MCP 或重启 Agent**：stdio 服务的工具清单在进程启动时固定，改了源码不重载就看不到新工具、也仍跑的是旧代码。

## 日常使用

在 Agent 里直接说「同步代码到 ubuntu」。标准流程：

1. `vm_sync_preview` 扫描并签发令牌，返回本批窗口（最多 100 个文件）
2. `vm_sync_detail` 查看 `add` / `overwrite` / `remote_only` / `conflicts` 等分类明细
3. 你明确批准后，`vm_sync_apply` 携带 `token` + `confirm_digest` 执行上传
4. `vm_sync_status` 复核结果

编译：`vm_cmake_configure` → `vm_build` → `vm_run_ctest`。需要一次性回归用 `vm_verify_stage6`。

### 同步行为要点

- **单向**：只上传本地存在的文件，远端多出来的文件归为 `remote_only`，只列出、**不删除**
- **哈希校验**：每个文件先写 `.vm_sync-<32hex>.tmp` 再 rename，落地后比对 SHA256
- **输出截断差异**：`vm_exec` 的 stdout 超过 10 KB 会直接截断并附 `[truncated]`；`vm_collect_log` 是读完再按 `log_max_bytes` 截断（大文件仍会全量传输）
- **`.sh` 特殊处理**：上传前 CRLF 自动转 LF，落地权限 `0755`；其他文件原样，新文件 `0644`
- **令牌绑定**：有效期 900 秒，且绑定配置指纹与计划摘要；文件或配置一变，`apply` 直接返回 `rejected_stale` / `rejected_config_changed`，需重新 preview
- **分批**：超过 100 个文件返回 `batch_applied` + `has_more`，再 preview 取下一批
- **无单文件参数**：`apply` 按整个窗口执行；只想同步个别文件时用 `sync_excludes` 控制范围，或自行 scp

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `connected: false` | VM 未开机 / IP 或端口错 / `openssh-server` 没运行。先在本地 `ssh <user>@<host>` 验证 |
| 报主机密钥被拒绝 | VM 重装或指纹变化。按上文用 `ssh-keygen -R` 删旧条目，再重跑 `deploy_key.py` |
| `Permission denied (publickey)` | 公钥没进 `authorized_keys`，或 `ssh_key_path` 指错。重跑密钥脚本 |
| 密钥脚本报 `Incorrect padding` / 加载 known_hosts 失败 | 你的 known_hosts 含 `@cert-authority`、`@revoked` 或损坏行，paramiko 解析不了。人工清理这些行后重试 |
| `Invalid \escape: line 1 column ...` | `config.json` 里 Windows 路径用了单反斜杠，改正斜杠或双反斜杠；经同步配置加载时报的是 `invalid sync config: Invalid \escape: ...` |
| `getaddrinfo failed` / `Name or service not known` | `ssh_host` 占位符没替换，或地址不可达（异常类型是 `socket.gaierror`，但输出文案里没有这个词） |
| `invalid sync config: ...` | `config.json` 字段缺失或类型错，按提示修改；路径必须是绝对路径且本地目录存在 |
| `rejected_stale` / `rejected_config_changed` | 预览后文件或配置变了，重新 preview 取新 `confirm_digest` |
| 工具调用超时但远端文件已写入 | 旧版本 PTY 缺陷（已修复）：升级到本版本并重载 MCP |
| `command timed out after 120 seconds`（`exit_code=124`） | 命令超时预算不够，调大 `command_timeout_seconds`；注意远端命令仍在继续执行，并没有被中止 |
| 需要 tty 的远端交互脚本行为异常 | 已知限制：`vm_exec` 不再分配 PTY（为了正确送达 EOF），需要 tty 的命令请自行 `ssh -t` 执行 |
| Agent 里看不到 `vm_sync_*` 工具 | MCP 进程是改动前启动的，重载 MCP 或重启 Agent |
| 第三方项目首次 `vm_verify_stage6` 构建失败 | 预期行为：`verify_targets` 默认是本项目目标（`vision_gui` 等），改成你自己的目标即可 |
| 编译报找不到编译器 | 远端工具链未装或 `build_dir` 里残留旧 CMakeCache，删掉远端构建目录后重新 configure |

## 安全说明

- 私钥只在本地生成，绝不上传；密码仅用于一次公钥部署，不落盘、不写日志、不打印
- `verify_host_keys` 默认 `true`，未知主机直接拒连，不做静默接受
- 同步层的路径校验拒绝符号链接与 `..` 逃逸，只写项目根内的普通文件；密钥脚本则在 `~/.ssh` 下用 `shlex.quote` 包串幂等追加，不适用项目根校验
- 错误消息会移除私钥路径；配置类型写错时的告警只打键名与类型名，不打配置值

## 开发

```bash
python -m pytest -q
```

当前基线：244 passed, 3 skipped。

## 许可证

MIT，见 `LICENSE`。
