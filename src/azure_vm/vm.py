from __future__ import annotations

import json
import shlex
import shutil
import socket
import time
from pathlib import Path

import paramiko

from ._backend import CommandBackend, CommandResult
from .exceptions import (
    AzureVmCommandError,
    AzureVmTimeoutError,
    SshConnectionError,
)
from .models import VmInfo


class AzureVM:
    def __init__(
        self,
        name: str,
        workspace_dir: Path,
        backend: CommandBackend,
        ssh_key_path: str | None = None,
        ssh_username: str = "azureuser",
    ) -> None:
        self.name = name
        self._workspace_dir = workspace_dir
        self._backend = backend
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username

    def _run(self, args: list[str]) -> CommandResult:
        result = self._backend.run(args, cwd=str(self._workspace_dir))
        if not result.success:
            raise AzureVmCommandError(
                result.args, result.returncode, result.stdout, result.stderr
            )
        return result

    def _ip(self) -> str:
        result = self._run(["tofu", "output", "-json"])
        data = json.loads(result.stdout)
        ip = data.get("vm_ip", {}).get("value", "")
        if ip:
            return ip
        return ""

    # ------------------------------------------------------------ lifecycle

    def info(self) -> VmInfo:
        result = self._run(["tofu", "output", "-json"])
        return VmInfo.from_tofu_output(json.loads(result.stdout), self.name)

    def start(self) -> None:
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=running"])

    def stop(self) -> None:
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=stopped"])

    def restart(self) -> None:
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=restart"])

    def delete(self) -> None:
        self._run(["tofu", "destroy", "-auto-approve"])

    # ---------------------------------------------------------------- SSH

    def _ssh_client(self) -> paramiko.SSHClient:
        ip = self._ip()
        if not ip:
            raise AzureVmTimeoutError(self.name, 0)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=ip,
                username=self._ssh_username,
                key_filename=self._ssh_key_path,
                timeout=10,
            )
        except (OSError, paramiko.SSHException) as e:
            ssh.close()
            raise SshConnectionError(self.name, ip, str(e)) from e
        return ssh

    def exec(self, command: list[str]) -> CommandResult:
        ssh = self._ssh_client()
        try:
            _, stdout, stderr = ssh.exec_command(shlex.join(command))
            exit_status = stdout.channel.recv_exit_status()
            result = CommandResult(
                args=command,
                returncode=exit_status,
                stdout=stdout.read().decode("utf-8", errors="replace"),
                stderr=stderr.read().decode("utf-8", errors="replace"),
            )
            return result
        finally:
            ssh.close()

    def exec_structured(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        parts: list[str] = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        for k, v in (env or {}).items():
            parts.append(f"export {k}={shlex.quote(v)}")
        parts.append(shlex.join(argv))
        command = " && ".join(parts)
        return self.exec(["bash", "-lc", command])

    def transfer(self, source: str, dest: str) -> None:
        ssh = self._ssh_client()
        try:
            sftp = ssh.open_sftp()
            if ":" in source:
                sftp.get(source, dest)
            else:
                sftp.put(source, dest)
            sftp.close()
        finally:
            ssh.close()

    # --------------------------------------------------------------- clone

    def clone(self, new_name: str) -> "AzureVM":
        new_ws = self._workspace_dir.parent / new_name
        if self._workspace_dir.exists():
            shutil.copytree(self._workspace_dir, new_ws, dirs_exist_ok=True)
        else:
            new_ws.mkdir(parents=True, exist_ok=True)
        self._backend.run(
            ["tofu", "apply", "-auto-approve", "-var", f"vm_name={new_name}"],
            cwd=str(new_ws),
        )
        return AzureVM(
            new_name,
            new_ws,
            self._backend,
            self._ssh_key_path,
            self._ssh_username,
        )

    # --------------------------------------------------------- wait_for_ip

    def wait_for_ip(self, timeout: float = 120, *, interval: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = self._ip()
            if ip:
                return ip
            time.sleep(interval)
        raise AzureVmTimeoutError(self.name, timeout)

    # ---------------------------------------------------------- wait_ready

    def wait_ready(
        self, timeout: float = 120, port: int = 22, *, interval: float = 2.0
    ) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = self._ip()
            if ip:
                try:
                    with socket.create_connection((ip, port), timeout=1):
                        return ip
                except OSError:
                    pass
            time.sleep(interval)
        raise AzureVmTimeoutError(self.name, timeout)
