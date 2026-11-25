# 真机验收清单

环境：本地 Windows（干净用户目录）+ Ubuntu 虚拟机（已装 openssh-server、cmake、项目工具链）。
本机未装 cmake/opencv/sqlite3/Qt5，涉及远端编译的步骤必须在 Ubuntu 侧执行。

## 1. 安装与密钥

1. 克隆仓库到全新目录，执行 `python -m pip install -r requirements.txt`。
   期望：无报错，`python -c "import mcp, paramiko, cryptography"` 成功。
2. 执行 `python scripts/deploy_key.py --host <VM_IP> --user <VM_USER>`，输入一次密码。
   期望：输出 `Host key pinned: SHA256:<43位base64>` 与 `Private key: <路径>`，退出码 0。
3. 在 VM 上执行 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`。
   期望：其 SHA256 base64 与第 2 步打印值一致（不一致说明遭遇中间人，按第 5 步删条目后重跑）。
4. 再执行一次同一命令。
   期望：同样成功；远端 `grep -c ubuntu-vm-mcp ~/.ssh/authorized_keys` 结果为 1（不重复追加）；
   本地 `~/.ssh/known_hosts` 中该主机的行数仍为 1（不重复追加指纹行）。
5. 手工把 known_hosts 里该主机的 base64 改成 `AAAAold`，再跑一次脚本。
   期望：报 `host key mismatch for <entry>`，退出码 1，且 known_hosts **未被改写**；
   用 `ssh-keygen -R <entry>`（非 22 端口写成 `"[ip]:port"`）删掉后重跑即恢复正常。
6. 在 known_hosts 同主机保留一行 `ecdsa-sha2-nistp256` 条目（模拟正常机器多密钥类型），重跑脚本。
   期望：不报 mismatch，正常追加 ed25519 条目。
7. 本地执行 `ssh -i <私钥路径> <VM_USER>@<VM_IP> "echo ok"`。
   期望：输出 `ok`，不再索要密码。

## 2. 配置与 Agent 接入

8. 复制 `config.example.json` 为 `config.json`，替换全部 `<...>` 占位符（Windows 路径写正斜杠）。
9. 故意只留 `ssh_host` 为 `<VM_IP>` 不替换，调用 `vm_check_connection`。
   期望：`connected: false`，错误信息里是 `gaierror` 类字样（说明占位符没换，不是网络故障）；改回后恢复。
10. 把 mcp 片段贴进所用 Agent 配置，改 `command` 与 `args` 两条绝对路径；VS Code 用 `servers` 键。
11. **重载 MCP 或重启 Agent**（stdio 进程不重载就仍跑旧代码）。
    期望：Agent 工具列表出现 13 个工具，含 5 个 `vm_sync_*`。
12. 调用 `vm_check_connection`。
    期望：`connected: true`，返回主机名与内核版本。

## 3. stdin EOF 回归（Task 1 的真机验证）

13. 调用 `vm_exec`，command 为 `cat > /tmp/eof_check.txt; wc -c /tmp/eof_check.txt`，stdin_text 为 `HELLO123`。
    期望：**立刻返回**，stdout 含 `9 /tmp/eof_check.txt`，且 stdout 中**不含**回显的 `HELLO123`。
    修复前的表现：调用超时，但远端文件已写入 9 字节，且输出里混有终端回显。
14. 调用 `vm_exec`，command 为 `python3 -c "import os;print(os.isatty(0))"`。
    期望：输出 `False`（不再分配 PTY）。需要 tty 的交互脚本请改用 `ssh -t`。
15. 清理：`vm_exec` 执行 `rm -f /tmp/eof_check.txt`。

## 4. 同步闭环

16. 在本地项目改一个源文件（例如追加一行注释），调用 `vm_sync_preview`。
    期望：`status: ready`，该文件出现在 `overwrite` 分类，返回 `token` 与 `confirm_digest`。
17. 调用 `vm_sync_detail` 查看该分类，确认路径与哈希。
18. 经使用者明确批准后调用 `vm_sync_apply`，携带上一步的 `token` 与 `confirm_digest`。
    期望：`status: all_applied`，`failed` 为空。
19. 两端哈希比对：本地 `Get-FileHash <文件> -Algorithm SHA256`，远端 `sha256sum <文件>`。
    期望：两个哈希完全一致（`.sh` 文件比对的是 CRLF 转 LF 之后的上传哈希）。
20. 调用 `vm_sync_status`。
    期望：该文件归入 `unchanged`。
21. 用旧 `confirm_digest` 配新 `token`（或改文件后再 apply）。
    期望：返回 `rejected_stale`，远端文件未被改动。
22. 在 `config.json` 的 `sync_excludes` 里加入某个文件路径后重新 preview。
    期望：该文件不再出现在 `add`/`overwrite`，而被计入排除统计。

## 5. 编译与阶段校验

23. 调用 `vm_cmake_configure`。
    期望：`success: true` 且 `summary` 为 `exit_code=0`（该工具返回键是 `success/summary/errors/full_output`，没有独立的 `exit_code` 键）。
24. 调用 `vm_build`。期望：`success: true`，`error_lines` 为空。
25. 调用 `vm_run_ctest`。期望：`failed` 为 0。
26. 在 `config.json` 里把 `verify_targets` 改成你项目的一个目标、`verify_direct_tests` 与
    `verify_ctest_filter` 同步改小，调用 `vm_verify_stage6`。
    期望：构建的是你配置的目标，而不是默认的 `vision_gui` 等。
27. 把 `verify_targets` 改成 `[]`，调用 `vm_verify_stage6`。
    期望：日志出现 `verify_targets 为空，已回退默认值`，实际仍构建默认目标（空值不等于跳过）。

## 6. 开源卫生

28. `git log --all --oneline -- config.json`。期望：无输出。
29. `git ls-files config.json`。期望：无输出；`git ls-files config.example.json` 期望列出该文件。
30. `git status --short`。期望：无输出（`config.json`、`docs/superpowers/`、`.superpowers/`、`Report.md` 均被忽略）。
31. `python -m pytest -q`。期望：244 passed, 3 skipped。
32. `Get-Content .gitattributes`。期望：`* text=auto eol=lf`，且新克隆的 `.sh` 文件为 LF。
