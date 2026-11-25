import json
import paramiko
import pytest
from unittest.mock import patch, MagicMock, mock_open
from ssh_client import SSHClient

CONFIG = {
    "ssh_host": "127.0.0.1",
    "ssh_port": 2222,
    "ssh_user": "ubuntu",
    "ssh_key_path": "/fake/key",
    "project_root": "/home/ubuntu/project",
    "build_dir": "build",
    "command_timeout_seconds": 10,
    "log_max_bytes": 20480,
}


def build_client():
    with patch("builtins.open", mock_open(read_data=json.dumps(CONFIG))):
        return SSHClient("/fake/config.json")


def test_exec_returns_tuple():
    client = build_client()
    mock_stdin = MagicMock()
    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.read.return_value = b"ok\n"
    mock_stdout.channel = mock_channel
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""

    with patch("paramiko.SSHClient") as mock_paramiko:
        inst = mock_paramiko.return_value
        inst.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        client._client = inst
        out, err, code = client.exec("echo ok")

    assert out == "ok\n"
    assert err == ""
    assert code == 0


def test_properties_loaded():
    client = build_client()
    assert client.project_root == "/home/ubuntu/project"
    assert client.build_dir == "build"
    assert client.log_max_bytes == 20480


def test_exec_reconnects_on_failure():
    client = build_client()

    mock_stdin = MagicMock()
    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.read.return_value = b"ok\n"
    mock_stdout.channel = mock_channel
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""

    first_client = MagicMock()
    first_client.exec_command.side_effect = paramiko.SSHException("connection lost")

    second_client = MagicMock()
    second_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)

    client._client = first_client

    with patch("paramiko.SSHClient") as mock_cls:
        mock_cls.return_value = second_client
        out, err, code = client.exec("echo ok")

    assert out == "ok\n"
    assert code == 0


def test_close():
    client = build_client()
    mock_transport = MagicMock()
    client._client = mock_transport
    client.close()
    mock_transport.close.assert_called_once()


def test_exec_drains_output_before_waiting_for_exit_status():
    client = build_client()
    order = []

    mock_stdin = MagicMock()
    mock_channel = MagicMock()
    mock_channel.recv_exit_status.side_effect = lambda: order.append("exit") or 0
    mock_stdout = MagicMock()
    mock_stdout.read.side_effect = lambda: order.append("stdout") or b"ok\n"
    mock_stdout.channel = mock_channel
    mock_stderr = MagicMock()
    mock_stderr.read.side_effect = lambda: order.append("stderr") or b""

    with patch("paramiko.SSHClient") as mock_paramiko:
        inst = mock_paramiko.return_value
        inst.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        client._client = inst
        client.exec("echo ok")

    assert order == ["stdout", "stderr", "exit"]


def test_exec_writes_stdin_without_pty():
    client = build_client()
    mock_stdin = MagicMock()
    mock_stdin.channel = MagicMock()
    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.read.return_value = b"ok\n"
    mock_stdout.channel = mock_channel
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""

    with patch("paramiko.SSHClient") as mock_paramiko:
        inst = mock_paramiko.return_value
        inst.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        client._client = inst
        client.exec("bash startup.sh", stdin_text="3")

    inst.exec_command.assert_called_once_with("bash startup.sh", timeout=10, get_pty=False)
    mock_stdin.write.assert_called_once_with("3\n")
    mock_stdin.flush.assert_called_once()
    mock_stdin.channel.shutdown_write.assert_called_once()


def test_exec_returns_timeout_result():
    client = build_client()
    mock_stdin = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.read.side_effect = TimeoutError()
    mock_stdout.channel = MagicMock()
    mock_stderr = MagicMock()

    with patch("paramiko.SSHClient") as mock_paramiko:
        inst = mock_paramiko.return_value
        inst.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        client._client = inst
        out, err, code = client.exec("bash startup.sh")

    assert out == ""
    assert err == "command timed out after 10 seconds"
    assert code == 124


def test_open_sftp_sets_timeout_and_keepalive():
    client = build_client()
    client._client = MagicMock()
    sftp = MagicMock()
    client._client.open_sftp.return_value = sftp

    result = client.open_sftp(timeout_seconds=42)

    assert result is sftp
    sftp.get_channel.return_value.settimeout.assert_called_once_with(42)
    client._client.get_transport.return_value.set_keepalive.assert_called_once_with(30)


def test_close_clears_sftp_before_ssh():
    client = build_client()
    ssh = MagicMock()
    sftp = MagicMock()
    client._client = ssh
    client._sftp = sftp

    client.close()

    sftp.close.assert_called_once()
    ssh.close.assert_called_once()
    assert client._sftp is None
    assert client._client is None


def test_open_sftp_reconnects_once_after_stale_session():
    client = build_client()
    stale_sftp = MagicMock()
    stale_sftp.get_channel.return_value.closed = True
    original_ssh = MagicMock()
    fresh_ssh = MagicMock()
    fresh_sftp = MagicMock()
    client._client = original_ssh
    client._sftp = stale_sftp
    original_ssh.open_sftp.side_effect = paramiko.SSHException("lost")
    client._connect = MagicMock(side_effect=lambda: setattr(client, "_client", fresh_ssh))
    fresh_ssh.open_sftp.return_value = fresh_sftp

    result = client.open_sftp(timeout_seconds=42)

    assert result is fresh_sftp
    assert client._connect.call_count == 1
    stale_sftp.close.assert_called_once()


def test_open_sftp_reuses_live_session():
    client = build_client()
    client._client = MagicMock()
    sftp = MagicMock()
    sftp.get_channel.return_value.closed = False
    client._sftp = sftp

    result = client.open_sftp(timeout_seconds=42)

    assert result is sftp
    client._client.open_sftp.assert_not_called()
    sftp.get_channel.return_value.settimeout.assert_called_once_with(42)


def test_open_sftp_raises_after_one_failed_reconnect():
    client = build_client()
    client._client = MagicMock()
    client._client.open_sftp.side_effect = paramiko.SSHException("lost")
    fresh_ssh = MagicMock()
    fresh_ssh.open_sftp.side_effect = OSError("still down")
    client._connect = MagicMock(side_effect=lambda: setattr(client, "_client", fresh_ssh))

    with pytest.raises(OSError, match="still down"):
        client.open_sftp(timeout_seconds=42)

    assert client._connect.call_count == 1
    assert client._sftp is None
    assert client._client is None


def test_close_still_closes_ssh_when_sftp_close_fails():
    client = build_client()
    ssh = MagicMock()
    sftp = MagicMock()
    sftp.close.side_effect = OSError("sftp close failed")
    client._client = ssh
    client._sftp = sftp

    client.close()

    ssh.close.assert_called_once()
    assert client._sftp is None
    assert client._client is None


def test_open_sftp_closes_session_when_channel_configuration_fails():
    client = build_client()
    ssh = MagicMock()
    sftp = MagicMock()
    sftp.get_channel.return_value.settimeout.side_effect = OSError("timeout setup failed")
    ssh.open_sftp.return_value = sftp
    client._client = ssh
    client._connect = MagicMock(side_effect=OSError("reconnect failed"))

    with pytest.raises(OSError, match="reconnect failed"):
        client.open_sftp(timeout_seconds=42)

    sftp.close.assert_called_once()
    assert client._sftp is None


def test_open_sftp_uses_default_timeout():
    client = build_client()
    client._client = MagicMock()
    sftp = MagicMock()
    client._client.open_sftp.return_value = sftp

    client.open_sftp()

    sftp.get_channel.return_value.settimeout.assert_called_once_with(300)


def test_open_sftp_connects_when_client_missing():
    client = build_client()
    ssh = MagicMock()
    sftp = MagicMock()
    stale_sftp = MagicMock()
    ssh.open_sftp.return_value = sftp
    client._sftp = stale_sftp
    client._connect = MagicMock(side_effect=lambda: setattr(client, "_client", ssh))

    result = client.open_sftp(timeout_seconds=42)

    assert result is sftp
    client._connect.assert_called_once_with()
    stale_sftp.close.assert_called_once()


def test_exec_still_reconnects_when_sftp_close_fails():
    client = build_client()
    bad_sftp = MagicMock()
    bad_sftp.close.side_effect = OSError("sftp close failed")
    dead_ssh = MagicMock()
    dead_ssh.exec_command.side_effect = paramiko.SSHException("lost")
    client._client = dead_ssh
    client._sftp = bad_sftp
    fresh_ssh = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = b"ok"
    stdout.channel.recv_exit_status.return_value = 0
    stderr = MagicMock()
    stderr.read.return_value = b""
    fresh_ssh.exec_command.return_value = (MagicMock(), stdout, stderr)
    client._connect = MagicMock(side_effect=lambda: setattr(client, "_client", fresh_ssh))

    out, err, code = client.exec("echo ok")

    assert (out, err, code) == ("ok", "", 0)
    client._connect.assert_called_once_with()
    bad_sftp.close.assert_called_once()


def test_close_ignores_ssh_close_failure():
    client = build_client()
    ssh = MagicMock()
    ssh.close.side_effect = OSError("ssh close failed")
    client._client = ssh

    client.close()

    assert client._client is None


def test_open_sftp_allows_missing_transport():
    client = build_client()
    client._client = MagicMock()
    client._client.get_transport.return_value = None
    sftp = MagicMock()
    client._client.open_sftp.return_value = sftp

    assert client.open_sftp(timeout_seconds=42) is sftp
