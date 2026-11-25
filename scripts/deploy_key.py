"""一次性生成 SSH 密钥、固定主机指纹并部署公钥的最小脚本。

只做四件事：生成 ed25519 密钥、抓取并固定远端主机指纹、用一次密码把公钥幂等
追加到远端 authorized_keys、验证密钥免密登录。不创建虚拟环境、不写 Agent 配置、
不做交互问答编排。密码仅在内存中使用一次，不落盘、不写日志。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import shlex
import socket
import sys
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_COMMENT = "ubuntu-vm-mcp"
LOGIN_PROBE = "echo ubuntu-vm-mcp-ok"
LOGIN_PROBE_MARKER = "ubuntu-vm-mcp-ok"
TCP_CONNECT_TIMEOUT_SECONDS = 15
HANDSHAKE_TIMEOUT_SECONDS = 15


def _fingerprint(server_key: paramiko.PKey) -> str:
    """返回 OpenSSH 标准 SHA256 指纹（base64、无填充），可直接与 ssh-keygen -lf 对照。"""
    digest = hashlib.sha256(base64.b64decode(server_key.get_base64())).digest()
    return base64.b64encode(digest).decode().rstrip("=")


def _pinned_base64(lines: list[str], entry: str) -> str | None:
    """在 known_hosts 行中查找 entry 已钉的公钥 base64。

    标记行（@cert-authority/@revoked）、注释行与字段不足的行一律跳过：
    paramiko 的 HostKeys.load 会在标记行上抛 InvalidHostKey，而且它不是 SSHException 的子类，兜不住。
    """
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("@"):
            continue
        fields = stripped.split()
        if len(fields) < 3:
            continue
        if entry in fields[0].split(","):
            return fields[2]
    return None


def generate_key(key_path: Path) -> str:
    """生成或复用 ed25519 密钥对，写出 .pub 文件并返回 OpenSSH 公钥单行文本。"""
    key_path = Path(key_path).expanduser()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        # paramiko 5.0.0 没有 Ed25519Key.generate()，只能用 cryptography 生成再交给 paramiko 加载
        private_pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        key_path.write_bytes(private_pem)
        key_path.chmod(0o600)
    loaded = paramiko.Ed25519Key.from_private_key_file(str(key_path))
    public_line = f"{loaded.get_name()} {loaded.get_base64()} {KEY_COMMENT}"
    Path(str(key_path) + ".pub").write_text(public_line + "\n", encoding="utf-8")
    return public_line


def pin_host_key(host: str, port: int, known_hosts: Path) -> str:
    """抓取远端主机公钥并固定到 known_hosts，返回 OpenSSH 标准 SHA256 指纹。

    只用 append 写单行，绝不整份重写：paramiko HostKeys.save 是 truncate+重写，
    会丢掉它解析不了的行（如 sk-ssh-ed25519、@cert-authority），在 Windows 上还会把整份文件改成 CRLF。
    """
    known_hosts = Path(known_hosts).expanduser()
    entry = host if port == 22 else f"[{host}]:{port}"

    # Transport((host, port)) 内部直接阻塞 connect，不带任何超时，IP 写错会长时间假死
    sock = socket.create_connection((host, port), timeout=TCP_CONNECT_TIMEOUT_SECONDS)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=HANDSHAKE_TIMEOUT_SECONDS)
        server_key = transport.get_remote_server_key()
    finally:
        transport.close()

    lines: list[str] = []
    if known_hosts.exists():
        lines = known_hosts.read_text(encoding="utf-8", errors="replace").splitlines()

    pinned = _pinned_base64(lines, entry)
    if pinned is not None and pinned != server_key.get_base64():
        raise RuntimeError(
            f"host key mismatch for {entry}: pinned fingerprint differs from the server key. "
            f"If the VM was rebuilt, remove the stale line from {known_hosts} and run again."
        )

    if pinned is None:
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        newline = "\n" if not lines or lines[-1].strip() else ""
        with open(known_hosts, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{newline}{entry} {server_key.get_name()} {server_key.get_base64()}\n")

    return _fingerprint(server_key)


def _verified_client(known_hosts: Path) -> paramiko.SSHClient:
    """创建只接受 known_hosts 中已固定指纹的 SSH 客户端。"""
    client = paramiko.SSHClient()
    client.load_host_keys(str(Path(known_hosts).expanduser()))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    return client


def deploy_public_key(
    host: str, port: int, user: str, password: str, public_line: str, known_hosts: Path
) -> None:
    """用一次密码登录，把公钥幂等追加到远端 authorized_keys 并收紧权限。"""
    client = _verified_client(known_hosts)
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        quoted = shlex.quote(public_line)
        # umask 只影响新建文件，已存在的 ~/.ssh 与 authorized_keys 必须显式 chmod，
        # 否则 OpenSSH 的 StrictModes 仍可能拒绝密钥登录
        command = (
            "umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
            "touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; "
            f"grep -qxF {quoted} ~/.ssh/authorized_keys || echo {quoted} >> ~/.ssh/authorized_keys"
        )
        _, stdout, stderr = client.exec_command(command, timeout=30)
        code = stdout.channel.recv_exit_status()
        if code != 0:
            detail = stderr.read().decode(errors="replace").strip()
            raise RuntimeError(detail or f"authorized_keys update failed with exit code {code}")
    finally:
        client.close()


def verify_key_login(host: str, port: int, user: str, key_path: Path, known_hosts: Path) -> bool:
    """用密钥登录并执行一次 echo，确认免密通道可用。"""
    client = _verified_client(known_hosts)
    client.connect(
        hostname=host,
        port=port,
        username=user,
        key_filename=str(Path(key_path).expanduser()),
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        _, stdout, _ = client.exec_command(LOGIN_PROBE, timeout=15)
        return stdout.read().decode().strip() == LOGIN_PROBE_MARKER
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    """命令行入口：生密钥 -> 固定指纹 -> 一次密码部署公钥 -> 验证免密。"""
    parser = argparse.ArgumentParser(
        description="Generate an SSH key, pin the VM host key and deploy the public key."
    )
    parser.add_argument("--host", required=True, help="Ubuntu VM address")
    parser.add_argument("--user", required=True, help="Ubuntu VM login user")
    parser.add_argument("--port", type=int, default=22, help="SSH port, default 22")
    parser.add_argument("--key-path", default=str(Path.home() / ".ssh" / "ubuntu_vm_mcp_key"))
    args = parser.parse_args(argv)

    key_path = Path(args.key_path).expanduser()
    # 固定使用 ~/.ssh/known_hosts：ssh_client.py 只读该路径，避免出现两个真源
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    try:
        public_line = generate_key(key_path)
        fingerprint = pin_host_key(args.host, args.port, known_hosts)
        print(f"Host key pinned: SHA256:{fingerprint}")
        password = getpass.getpass(f"Password for {args.user}@{args.host} (used once, not stored): ")
        deploy_public_key(args.host, args.port, args.user, password, public_line, known_hosts)
        if not verify_key_login(args.host, args.port, args.user, key_path, known_hosts):
            print("Key login verification failed.", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Private key: {key_path}")
    print("Next: copy config.example.json to config.json, then fill ssh_host / ssh_user /")
    print("      ssh_key_path / project_root / local_project_root.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
