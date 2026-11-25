import json
import logging
from pathlib import Path

import paramiko


class SSHClient:
    def __init__(self, config_path=None):
        cfg = Path(config_path) if config_path else Path(__file__).parent / "config.json"
        with open(cfg, "r", encoding="utf-8") as f:
            self._config = json.load(f)
        self._client = None
        self._sftp = None

    def _connect(self):
        client = paramiko.SSHClient()
        verify = self._config.get("verify_host_keys", True)
        if verify:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            known_hosts = Path.home() / ".ssh" / "known_hosts"
            if known_hosts.exists():
                client.load_host_keys(str(known_hosts))
        else:
            client.set_missing_host_key_policy(paramiko.WarningPolicy())
        client.connect(
            hostname=self._config["ssh_host"],
            port=int(self._config["ssh_port"]),
            username=self._config["ssh_user"],
            key_filename=self._config["ssh_key_path"],
            timeout=10,
        )
        self._client = client

    def exec(self, command, timeout=None, stdin_text=None):
        if self._client is None:
            self._connect()
        actual_timeout = timeout if timeout is not None else int(self._config.get("command_timeout_seconds", 120))
        try:
            return self._exec_once(command, actual_timeout, stdin_text=stdin_text)
        except TimeoutError:
            return "", f"command timed out after {actual_timeout} seconds", 124
        except (paramiko.SSHException, OSError, EOFError) as e:
            logging.warning("SSH exec failed, reconnecting: %s", e)
            self.close()
            self._connect()
            try:
                return self._exec_once(command, actual_timeout, stdin_text=stdin_text)
            except TimeoutError:
                return "", f"command timed out after {actual_timeout} seconds", 124

    def _exec_once(self, command, timeout, stdin_text=None):
        # 必须固定关闭 PTY：PTY 通道上 shutdown_write() 不产生 EOF，
        # cat 这类读到 EOF 的命令会一直挂到工具超时，而且终端回显会污染 stdout
        stdin, stdout, stderr = self._client.exec_command(
            command,
            timeout=timeout,
            get_pty=False,
        )
        if stdin_text is not None:
            payload = stdin_text if stdin_text.endswith("\n") else f"{stdin_text}\n"
            stdin.write(payload)
            stdin.flush()
            if hasattr(stdin, "channel"):
                stdin.channel.shutdown_write()
            else:
                stdin.close()
        output = stdout.read().decode()
        error_output = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        return output, error_output, code

    def open_sftp(self, timeout_seconds: int = 300) -> paramiko.SFTPClient:
        """返回可用 SFTP 会话，失效时关闭旧资源并重连一次。"""
        if self._client is None:
            self.close()
            self._connect()
        try:
            if self._sftp is None or self._sftp.get_channel().closed:
                if self._sftp is not None:
                    self._sftp.close()
                    self._sftp = None
                return self._open_sftp_once(timeout_seconds)
            self._sftp.get_channel().settimeout(timeout_seconds)
            return self._sftp
        except (paramiko.SSHException, OSError, EOFError) as exc:
            logging.warning("SSH SFTP failed, reconnecting: %s", exc)
            self.close()
            self._connect()
            try:
                return self._open_sftp_once(timeout_seconds)
            except (paramiko.SSHException, OSError, EOFError):
                self.close()
                raise

    def _open_sftp_once(self, timeout_seconds: int) -> paramiko.SFTPClient:
        """创建 SFTP 并设置通道超时和 SSH 保活。"""
        sftp = self._client.open_sftp()
        self._sftp = sftp
        sftp.get_channel().settimeout(timeout_seconds)
        transport = self._client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        return sftp

    @property
    def project_root(self):
        return self._config["project_root"]

    @property
    def build_dir(self):
        return self._config.get("build_dir", "build")

    @property
    def log_max_bytes(self):
        return int(self._config.get("log_max_bytes", 20480))

    @property
    def verify_targets(self):
        """阶段校验的构建目标列表；未配置时返回 None，由调用方回退默认值。"""
        return self._config.get("verify_targets")

    @property
    def verify_ctest_filter(self):
        """阶段校验的 ctest 过滤表达式；未配置时返回 None。"""
        return self._config.get("verify_ctest_filter")

    @property
    def verify_direct_tests(self):
        """阶段校验中直接运行的测试可执行文件列表；未配置时返回 None。"""
        return self._config.get("verify_direct_tests")

    def close(self):
        """尽力关闭并清空 SFTP/SSH 资源，清理异常只记录不阻断恢复流程。"""
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception as exc:
                logging.warning("SFTP close failed: %s", exc)
            finally:
                self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                logging.warning("SSH close failed: %s", exc)
            finally:
                self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
