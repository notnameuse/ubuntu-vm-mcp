import json
from pathlib import Path

import paramiko


class SSHClient:
    def __init__(self, config_path=None):
        cfg = Path(config_path) if config_path else Path(__file__).parent / "config.json"
        with open(cfg, "r", encoding="utf-8") as f:
            self._config = json.load(f)
        self._client = None

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self._config["ssh_host"],
            port=int(self._config["ssh_port"]),
            username=self._config["ssh_user"],
            key_filename=self._config["ssh_key_path"],
            timeout=10,
        )
        self._client = client

    def exec(self, command, timeout=None):
        if self._client is None:
            self._connect()
        actual_timeout = timeout if timeout is not None else int(self._config.get("command_timeout_seconds", 120))
        try:
            _, stdout, stderr = self._client.exec_command(command, timeout=actual_timeout)
            code = stdout.channel.recv_exit_status()
            return stdout.read().decode(), stderr.read().decode(), code
        except Exception:
            self._client = None
            self._connect()
            _, stdout, stderr = self._client.exec_command(command, timeout=actual_timeout)
            code = stdout.channel.recv_exit_status()
            return stdout.read().decode(), stderr.read().decode(), code

    @property
    def project_root(self):
        return self._config["project_root"]

    @property
    def log_max_bytes(self):
        return int(self._config.get("log_max_bytes", 20480))

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
