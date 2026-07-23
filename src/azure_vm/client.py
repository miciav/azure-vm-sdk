from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ._backend import CommandBackend, TofuBackend, run_command
from ._discovery import list_images, list_sizes as _list_sizes
from ._workspace import Workspace
from .exceptions import AzureVmCommandError, VmNotFoundError
from .models import ImageInfo, VmConfig, VmInfo, VmSize, VmState
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
        ssh_connect_timeout: float = 15.0,
        ssh_keepalive_interval: float = 30.0,
    ) -> None:
        self._resource_group = resource_group
        self._location = location
        self._backend: CommandBackend = backend or TofuBackend()
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username
        self._ssh_connect_timeout = ssh_connect_timeout
        self._ssh_keepalive_interval = ssh_keepalive_interval

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
            ssh_connect_timeout=self._ssh_connect_timeout,
            ssh_keepalive_interval=self._ssh_keepalive_interval,
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
        open_ports: list[int] | tuple[int, ...] | None = None,
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
            open_ports=open_ports,
        )

        wdir = str(self._workspace.vm_dir(name))
        run_command(self._backend, ["tofu", "init"], cwd=wdir)
        run_command(self._backend, ["tofu", "apply", "-auto-approve"], cwd=wdir)

        return self.get_vm(name)

    # ----------------------------------------------------------- launch_many

    def launch_many(
        self,
        configs: list[VmConfig],
        *,
        max_workers: int | None = None,
    ) -> list[AzureVM]:
        if not configs:
            return []

        workers = max_workers if max_workers is not None else len(configs)
        created: list[AzureVM] = []
        first_error: BaseException | None = None

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self.launch,
                    name=cfg.name,
                    vm_size=cfg.vm_size,
                    disk_size_gb=cfg.disk_size_gb,
                    image_urn=cfg.image_urn,
                    cloud_init_config=cfg.cloud_init_config,
                    ssh_key_path=cfg.ssh_key_path,
                    open_ports=cfg.open_ports,
                ): cfg
                for cfg in configs
            }

            for fut in as_completed(futures):
                if first_error is not None:
                    continue
                exc = fut.exception()
                if exc is not None:
                    first_error = exc
                    for pending in futures:
                        pending.cancel()
                else:
                    created.append(fut.result())

        if first_error is not None:
            with ThreadPoolExecutor(max_workers=max(len(created), 1)) as rollback:
                for rf in [rollback.submit(vm.delete) for vm in created]:
                    try:
                        rf.result()
                    except Exception:
                        pass
            raise first_error

        return created

    # ---------------------------------------------------------------- list

    def list(self) -> list[VmInfo]:
        return self._workspace.list_vms()

    # ---------------------------------------------------------------- find

    def find(self, publisher: str = "Canonical") -> list[ImageInfo]:
        return list_images(self._backend, publisher)

    # ----------------------------------------------------------- list_sizes

    def list_sizes(self, location: str | None = None) -> list[VmSize]:
        loc = location or self._location
        if not loc:
            raise AzureVmCommandError(
                [], -1, "",
                "location is required — pass it or set via AzureClient() "
                "or AZURE_LOCATION env var"
            )
        return _list_sizes(self._backend, loc)

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
        open_ports: list[int] | tuple[int, ...] | None = None,
    ) -> AzureVM:
        if not self._workspace.vm_exists(name):
            return self.launch(
                name=name, vm_size=vm_size, disk_size_gb=disk_size_gb,
                image_urn=image_urn, cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path, open_ports=open_ports,
            )

        if not self._resource_group or not self._location:
            raise AzureVmCommandError(
                [], -1, "",
                "resource_group and location are required — set via AzureClient() "
                "or AZURE_RESOURCE_GROUP / AZURE_LOCATION env vars"
            )
        # Re-render the workspace from the CURRENT configuration: a pre-existing
        # workspace must never silently pin stale parameters (e.g. an old
        # resource group baked into main.tf by a previous run).
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
            open_ports=open_ports,
        )

        vm = self.get_vm(name)
        try:
            info = vm.info()
        except (AzureVmCommandError, json.JSONDecodeError):
            return self.launch(
                name=name, vm_size=vm_size, disk_size_gb=disk_size_gb,
                image_urn=image_urn, cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path, open_ports=open_ports,
            )

        if info.state == VmState.RUNNING:
            return vm

        vm.start()
        return self.get_vm(name)
