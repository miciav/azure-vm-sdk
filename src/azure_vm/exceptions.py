class AzureVmError(Exception):
    """Base exception for all azure-vm-sdk errors."""


class AzureVmCommandError(AzureVmError):
    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Command {args} failed with exit code {returncode}: {stderr or stdout}"
        )


class TofuNotInstalledError(AzureVmError):
    def __init__(self) -> None:
        super().__init__(
            "OpenTofu not found. Install from https://opentofu.org"
        )


class VmNotFoundError(AzureVmError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"VM '{name}' not found")


class AzureVmTimeoutError(AzureVmError):
    def __init__(self, name: str, timeout: float) -> None:
        self.name = name
        self.timeout = timeout
        super().__init__(f"VM '{name}' did not become ready within {timeout}s")


class SshConnectionError(AzureVmError):
    def __init__(self, name: str, host: str, reason: str) -> None:
        self.name = name
        self.host = host
        self.reason = reason
        super().__init__(f"SSH connection to '{name}' ({host}) failed: {reason}")
