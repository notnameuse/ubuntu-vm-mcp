import json
import paramiko
from unittest.mock import patch, MagicMock, mock_open
from ssh_client import SSHClient

CONFIG = {
    "ssh_host": "127.0.0.1",
    "ssh_port": 2222,
    "ssh_user": "ubuntu",
    "ssh_key_path": "/fake/key",
    "project_root": "/home/ubuntu/project",
    "command_timeout_seconds": 10,
    "log_max_bytes": 20480,
}


def build_client():
    with patch("builtins.open", mock_open(read_data=json.dumps(CONFIG))):
        return SSHClient("/fake/config.json")


def test_exec_returns_tuple():
    client = build_client()
    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.read.return_value = b"ok\n"
    mock_stdout.channel = mock_channel
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""

    with patch("paramiko.SSHClient") as mock_paramiko:
        inst = mock_paramiko.return_value
        inst.exec_command.return_value = (None, mock_stdout, mock_stderr)
        client._client = inst
        out, err, code = client.exec("echo ok")

    assert out == "ok\n"
    assert err == ""
    assert code == 0


def test_properties_loaded():
    client = build_client()
    assert client.project_root == "/home/ubuntu/project"
    assert client.log_max_bytes == 20480


def test_exec_reconnects_on_failure():
    client = build_client()

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
    second_client.exec_command.return_value = (None, mock_stdout, mock_stderr)

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
