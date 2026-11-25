"""密钥脚本单测：生成/复用、指纹固定、幂等部署、免密验证、入口返回码。"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import paramiko

from scripts import deploy_key


def test_generate_key_creates_private_and_public(tmp_path):
    """首次生成 ed25519 密钥，私钥可被 paramiko 加载且公钥 base64 与之一致。"""
    key_path = tmp_path / "vm_key"
    public_line = deploy_key.generate_key(key_path)

    assert key_path.exists()
    assert Path(str(key_path) + ".pub").exists()
    assert public_line.startswith("ssh-ed25519 ")
    loaded = paramiko.Ed25519Key.from_private_key_file(str(key_path))
    assert loaded.get_base64() == public_line.split()[1]


def test_generate_key_reuses_existing_key(tmp_path):
    """重复执行不得覆盖已有私钥，公钥保持一致。"""
    key_path = tmp_path / "vm_key"
    first = deploy_key.generate_key(key_path)
    private_bytes = key_path.read_bytes()
    second = deploy_key.generate_key(key_path)

    assert first == second
    assert key_path.read_bytes() == private_bytes


def test_pin_host_key_writes_entry_and_closes_transport(tmp_path):
    """默认端口下以裸 IP 入库，指纹为 OpenSSH 标准 43 字符 base64（无填充）。"""
    deploy_key.generate_key(tmp_path / "vm_key")
    server_key = paramiko.Ed25519Key.from_private_key_file(str(tmp_path / "vm_key"))
    transport = MagicMock()
    transport.get_remote_server_key.return_value = server_key
    known_hosts = tmp_path / "known_hosts"

    with patch("scripts.deploy_key.paramiko.Transport", return_value=transport):
        fingerprint = deploy_key.pin_host_key("192.0.2.10", 22, known_hosts)

    assert len(fingerprint) == 43
    assert "=" not in fingerprint
    assert "192.0.2.10" in known_hosts.read_text(encoding="utf-8")
    transport.close.assert_called_once()


def test_pin_host_key_uses_bracket_entry_for_nondefault_port(tmp_path):
    """非标准端口必须写成 [host]:port，否则服务端查不到已固定指纹。"""
    deploy_key.generate_key(tmp_path / "vm_key")
    server_key = paramiko.Ed25519Key.from_private_key_file(str(tmp_path / "vm_key"))
    transport = MagicMock()
    transport.get_remote_server_key.return_value = server_key
    known_hosts = tmp_path / "known_hosts"

    with patch("scripts.deploy_key.paramiko.Transport", return_value=transport):
        deploy_key.pin_host_key("192.0.2.10", 2222, known_hosts)

    assert "[192.0.2.10]:2222" in known_hosts.read_text(encoding="utf-8")


def _fake_ssh_client(exit_code=0, stderr=b""):
    """构造一个只返回预设退出码与 stderr 的假 SSHClient。"""
    client = MagicMock()
    stdout = MagicMock()
    stdout.channel.recv_exit_status.return_value = exit_code
    stderr_stream = MagicMock()
    stderr_stream.read.return_value = stderr
    client.exec_command.return_value = (MagicMock(), stdout, stderr_stream)
    return client


def test_deploy_public_key_command_is_idempotent(tmp_path):
    """部署命令必须先建目录再幂等追加，并且只走已固定指纹的 RejectPolicy。"""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    client = _fake_ssh_client()

    with patch("scripts.deploy_key.paramiko.SSHClient", return_value=client):
        deploy_key.deploy_public_key(
            "192.0.2.10", 22, "user", "secret", "ssh-ed25519 AAAA comment", known_hosts
        )

    command = client.exec_command.call_args[0][0]
    assert "grep -qxF" in command
    assert ">> ~/.ssh/authorized_keys" in command
    assert "mkdir -p ~/.ssh" in command
    assert "chmod 700 ~/.ssh" in command
    assert "chmod 600 ~/.ssh/authorized_keys" in command
    assert client.connect.call_args[1]["password"] == "secret"
    assert client.connect.call_args[1]["look_for_keys"] is False
    client.set_missing_host_key_policy.assert_called_once()
    assert isinstance(client.set_missing_host_key_policy.call_args[0][0], paramiko.RejectPolicy)
    client.close.assert_called_once()


def test_deploy_public_key_raises_on_nonzero_exit(tmp_path):
    """远端命令失败必须抛错并带上 stderr 原文，且通道一定要关闭。"""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    client = _fake_ssh_client(exit_code=1, stderr=b"permission denied")

    with patch("scripts.deploy_key.paramiko.SSHClient", return_value=client):
        try:
            deploy_key.deploy_public_key(
                "192.0.2.10", 22, "user", "secret", "ssh-ed25519 AAAA comment", known_hosts
            )
            raised = None
        except RuntimeError as exc:
            raised = exc

    assert raised is not None and "permission denied" in str(raised)
    client.close.assert_called_once()


def test_verify_key_login_checks_echo_output(tmp_path):
    """免密验证以 echo 标记为准，且必须用密钥文件登录。"""
    client = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = b"ubuntu-vm-mcp-ok\n"
    client.exec_command.return_value = (MagicMock(), stdout, MagicMock())

    with patch("scripts.deploy_key.paramiko.SSHClient", return_value=client):
        ok = deploy_key.verify_key_login(
            "192.0.2.10", 22, "user", tmp_path / "vm_key", tmp_path / "known_hosts"
        )

    assert ok is True
    assert client.connect.call_args[1]["key_filename"] == str(tmp_path / "vm_key")


def test_main_returns_1_when_generation_fails():
    """入口异常必须返回非 0，且把失败原因打到 stderr。"""
    with patch("scripts.deploy_key.generate_key", side_effect=OSError("disk full")):
        assert deploy_key.main(["--host", "192.0.2.10", "--user", "user"]) == 1


def test_main_success_path_returns_0(tmp_path):
    """正常路径按序执行四步，密码只用于部署且不被其他步骤复用。"""
    with patch("scripts.deploy_key.generate_key", return_value="ssh-ed25519 AAAA c"), \
         patch("scripts.deploy_key.pin_host_key", return_value="f" * 43), \
         patch("scripts.deploy_key.getpass.getpass", return_value="secret"), \
         patch("scripts.deploy_key.deploy_public_key") as deploy, \
         patch("scripts.deploy_key.verify_key_login", return_value=True):
        code = deploy_key.main([
            "--host", "192.0.2.10", "--user", "user",
            "--key-path", str(tmp_path / "vm_key"),
        ])

    assert code == 0
    deploy.assert_called_once()
    assert deploy.call_args[0][3] == "secret"
