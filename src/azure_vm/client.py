from __future__ import annotations

import json
import uuid
from pathlib import Path

from ._backend import CommandBackend, TofuBackend, run_command
from ._discovery import list_images
from ._workspace import Workspace
from .exceptions import AzureVmCommandError, VmNotFoundError
from .models import ImageInfo, VmInfo, VmState
from .vm import AzureVM


class AzureClient:
    """Manages Azure VMs via OpenTofu across a shared workspace directory."""

    def __init__(
        self,
        resource_group: str | None = None,
        location: str | None = None,
        work_dir: str | None = None,
        backend: CommandBackend | None = None,
        ssh_key_path: str | None = None,
        ssh_username: str = "azureuser",
    ) -> None:
        self._resource_group = resource_group
        self._location = location
        self._backend: CommandBackend = backend or TofuBackend()
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username

        root = Path(work_dir) if work_dir else Path.home() / ".azure-vm-sdk"
        self._workspace = Workspace(root, self._backend)

    # ---------------------------------------------------------------- get_vm

    def get_vm(self, name: str) -> AzureVM:
        if not self._workspace.vm_exists(name):
            raise VmNotFoundError(name)
        return AzureVM(
            name,
            self._workspace.vm_dir(name),
            self._backend,
            self._ssh_key_path,
            self._ssh_username,
        )

    # --------------------------------------------------------------- launch

    def launch(
        self,
        name: str | None = None,
        *,
        vm_size: str = "Standard_B1s",
        disk_size_gb: int = 30,
        image_urn: str | None = None,
        cloud_init_config: dict | str | None = None,
        ssh_key_path: str | None = None,
    ) -> AzureVM:
        if not self._resource_group or not self._location:
            raise AzureVmCommandError(
                [], -1, "",
                "resource_group and location are required — set via AzureClient() "
                "or AZURE_RESOURCE_GROUP / AZURE_LOCATION env vars"
            )
        if name is None:
            name = uuid.uuid4().hex[:8]

        self._workspace.ensure_shared_infra(self._resource_group, self._location)
        self._workspace.write_vm_workspace(
            name=name,
            vm_size=vm_size,
            disk_size_gb=disk_size_gb,
            image_urn=image_urn,
            cloud_init_config=cloud_init_config,
            ssh_key_path=ssh_key_path,
            resource_group=self._resource_group,
            location=self._location,
            ssh_username=self._ssh_username,
            default_ssh_key=self._ssh_key_path,
        )

        wdir = str(self._workspace.vm_dir(name))
        run_command(self._backend, ["tofu", "init"], cwd=wdir)
        run_command(self._backend, ["tofu", "apply", "-auto-approve"], cwd=wdir)

        return self.get_vm(name)

    # ---------------------------------------------------------------- list

    def list(self) -> list[VmInfo]:
        return self._workspace.list_vms()

    # ---------------------------------------------------------------- find

    def find(self, publisher: str = "Canonical") -> list[ImageInfo]:
        return list_images(self._backend, publisher)

    # --------------------------------------------------------------- purge

    def purge(self) -> None:
        self._workspace.purge()

    # -------------------------------------------------------- ensure_running

    def ensure_running(
        self,
        name: str,
        *,
        vm_size: str = "Standard_B1s",
        disk_size_gb: int = 30,
        image_urn: str | None = None,
        cloud_init_config: dict | str | None = None,
        ssh_key_path: str | None = None,
    ) -> AzureVM:
        if not self._workspace.vm_exists(name):
            return self.launch(
                name=name, vm_size=vm_size, disk_size_gb=disk_size_gb,
                image_urn=image_urn, cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path,
            )

        vm = self.get_vm(name)
        try:
            info = vm.info()
        except (AzureVmCommandError, json.JSONDecodeError):
            return self.launch(
                name=name, vm_size=vm_size, disk_size_gb=disk_size_gb,
                image_urn=image_urn, cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path,
            )

        if info.state == VmState.RUNNING:
            return vm

        vm.start()
        return self.get_vm(name)
