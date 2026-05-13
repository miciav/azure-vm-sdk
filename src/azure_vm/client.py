from __future__ import annotations

import json
import uuid
from pathlib import Path

import yaml

from ._backend import CommandBackend, CommandResult, TofuBackend
from .exceptions import AzureVmCommandError
from .models import ImageInfo, VmInfo, VmState
from .vm import AzureVM


SHARED_TEMPLATE = """\
terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
}}

resource "azurerm_resource_group" "main" {{
  name     = "{resource_group}"
  location = "{location}"
}}
"""


VM_TEMPLATE = """\
terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
}}

data "azurerm_resource_group" "main" {{
  name = "{resource_group}"
}}

resource "azurerm_virtual_network" "vm" {{
  name                = "${{var.vm_name}}-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
}}

resource "azurerm_subnet" "vm" {{
  name                 = "${{var.vm_name}}-subnet"
  resource_group_name  = data.azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.vm.name
  address_prefixes     = ["10.0.1.0/24"]
}}

resource "azurerm_public_ip" "vm" {{
  name                = "${{var.vm_name}}-pip"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  allocation_method   = "Static"
}}

resource "azurerm_network_interface" "vm" {{
  name                = "${{var.vm_name}}-nic"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name

  ip_configuration {{
    name                          = "internal"
    subnet_id                     = azurerm_subnet.vm.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.vm.id
  }}
}}

resource "azurerm_linux_virtual_machine" "vm" {{
  name                  = var.vm_name
  location              = data.azurerm_resource_group.main.location
  resource_group_name   = data.azurerm_resource_group.main.name
  network_interface_ids = [azurerm_network_interface.vm.id]
  size                  = var.vm_size
  computer_name         = var.vm_name
  admin_username        = "{ssh_username}"
  disable_password_authentication = true

  admin_ssh_key {{
    username   = "{ssh_username}"
    public_key = file("{ssh_public_key_path}")
  }}

  os_disk {{
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = var.disk_size_gb
  }}

  source_image_reference {{
    publisher = "{image_publisher}"
    offer     = "{image_offer}"
    sku       = "{image_sku}"
    version   = "{image_version}"
  }}
{custom_data_block}
}}

variable "vm_name" {{
  type = string
}}

variable "vm_size" {{
  type    = string
  default = "Standard_B1s"
}}

variable "disk_size_gb" {{
  type    = number
  default = 30
}}

variable "desired_state" {{
  type    = string
  default = "running"
}}

output "vm_ip" {{
  value = azurerm_public_ip.vm.ip_address
}}

output "vm_state" {{
  value = var.desired_state
}}

output "location" {{
  value = data.azurerm_resource_group.main.location
}}

output "vm_size" {{
  value = var.vm_size
}}

output "image_urn" {{
  value = "{image_urn}"
}}

output "resource_group" {{
  value = "{resource_group}"
}}
"""


class AzureClient:
    """Manages Azure VMs via OpenTofu across a shared workspace directory.

    The client organises VMs under *work_dir*, each in its own sub-directory.
    Shared infrastructure (resource group) lives in ``work_dir/.shared/``.
    """

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
        self._work_dir = Path(work_dir) if work_dir else Path.home() / ".azure-vm-sdk"
        self._backend: CommandBackend = backend or TofuBackend()
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username

    # ------------------------------------------------------------------ util

    def _run(
        self, args: list[str], *, cwd: str | None = None
    ) -> CommandResult:
        result = self._backend.run(args, cwd=cwd)
        if not result.success:
            raise AzureVmCommandError(
                result.args, result.returncode, result.stdout, result.stderr
            )
        return result

    # ---------------------------------------------------------------- get_vm

    def get_vm(self, name: str) -> AzureVM:
        """Return an :class:`AzureVM` wrapper for the named VM."""
        return AzureVM(
            name,
            self._work_dir / name,
            self._backend,
            self._ssh_key_path,
            self._ssh_username,
        )

    # ------------------------------------------------------- shared infra

    def _ensure_shared_infra(self) -> None:
        """Create shared infrastructure (resource group) once."""
        shared_dir = self._work_dir / ".shared"
        if shared_dir.exists():
            return
        shared_dir.mkdir(parents=True)
        hcl = SHARED_TEMPLATE.format(
            resource_group=self._resource_group,
            location=self._location,
        )
        (shared_dir / "main.tf").write_text(hcl)
        self._run(["tofu", "init"], cwd=str(shared_dir))
        self._run(["tofu", "apply", "-auto-approve"], cwd=str(shared_dir))

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
        """Provision a new Azure VM.

        Parameters
        ----------
        name:
            VM name. A random name is generated when not provided.
        vm_size:
            Azure VM size (e.g. ``Standard_B1s``).
        disk_size_gb:
            OS disk size in GB.
        image_urn:
            Azure image URN in the form ``publisher:offer:sku:version``.
        cloud_init_config:
            Cloud-init configuration. A dict is serialised as YAML with the
            ``#cloud-config`` header. A raw string is written as-is.
        ssh_key_path:
            Path to the SSH public key to inject into the VM.

        Returns the :class:`AzureVM` instance.
        """
        if not self._resource_group or not self._location:
            raise AzureVmCommandError(
                [], -1, "",
                "resource_group and location are required — set via AzureClient() "
                "or AZURE_RESOURCE_GROUP / AZURE_LOCATION env vars"
            )

        if name is None:
            name = uuid.uuid4().hex[:8]

        self._ensure_shared_infra()

        workspace = self._work_dir / name
        self._write_vm_workspace(
            workspace,
            name,
            vm_size,
            disk_size_gb,
            image_urn,
            cloud_init_config,
            ssh_key_path,
        )

        self._run(["tofu", "init"], cwd=str(workspace))
        self._run(["tofu", "apply", "-auto-approve"], cwd=str(workspace))

        return self.get_vm(name)

    def _write_vm_workspace(
        self,
        workspace: Path,
        name: str,
        vm_size: str,
        disk_size_gb: int,
        image_urn: str | None,
        cloud_init_config: dict | str | None,
        ssh_key_path: str | None,
    ) -> None:
        """Write the HCL templates and variable files for a VM workspace."""
        workspace.mkdir(parents=True, exist_ok=True)

        # --- cloud-init ----------------------------------------------------
        cloud_init_block = ""
        if cloud_init_config is not None:
            if isinstance(cloud_init_config, dict):
                content = (
                    "#cloud-config\n"
                    + yaml.dump(cloud_init_config, default_flow_style=False)
                )
            else:
                content = cloud_init_config
            (workspace / "cloud-init.yaml").write_text(content)
            cloud_init_block = (
                '  custom_data = filebase64("cloud-init.yaml")\n'
            )

        # --- image URN parsing --------------------------------------------
        publisher: str = "Canonical"
        offer: str = "0001-com-ubuntu-server-noble"
        sku: str = "24_04-lts"
        version: str = "latest"
        if image_urn:
            parts = image_urn.split(":")
            if len(parts) > 0 and parts[0]:
                publisher = parts[0]
            if len(parts) > 1 and parts[1]:
                offer = parts[1]
            if len(parts) > 2 and parts[2]:
                sku = parts[2]
            if len(parts) > 3 and parts[3]:
                version = parts[3]

        # --- SSH key -------------------------------------------------------
        effective_ssh_path = str(Path(
            ssh_key_path or self._ssh_key_path or "~/.ssh/id_rsa.pub"
        ).expanduser())

        full_urn = (
            image_urn
            or "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest"
        )

        # --- main.tf -------------------------------------------------------
        hcl = VM_TEMPLATE.format(
            resource_group=self._resource_group,
            location=self._location,
            ssh_username=self._ssh_username,
            ssh_public_key_path=effective_ssh_path,
            image_publisher=publisher,
            image_offer=offer,
            image_sku=sku,
            image_version=version,
            image_urn=full_urn,
            custom_data_block=cloud_init_block,
        )
        (workspace / "main.tf").write_text(hcl)

        # --- terraform.tfvars ---------------------------------------------
        tfvars = (
            f'vm_name = "{name}"\n'
            f'vm_size = "{vm_size}"\n'
            f"disk_size_gb = {disk_size_gb}\n"
        )
        (workspace / "terraform.tfvars").write_text(tfvars)

    # ---------------------------------------------------------------- list

    def list(self) -> list[VmInfo]:
        """Return info for every VM workspace in the work directory."""
        vms: list[VmInfo] = []
        for item in sorted(self._work_dir.iterdir()):
            if item.name.startswith("."):
                continue
            if not item.is_dir():
                continue
            if not (item / "main.tf").exists():
                continue
            result = self._backend.run(
                ["tofu", "output", "-json"], cwd=str(item)
            )
            if result.success and result.stdout:
                try:
                    data = json.loads(result.stdout)
                    vms.append(VmInfo.from_tofu_output(data, item.name))
                except json.JSONDecodeError:
                    continue
        return vms

    # ---------------------------------------------------------------- find

    def find(self, publisher: str = "Canonical") -> list[ImageInfo]:
        """List available VM images from Azure Marketplace."""
        result = self._run(
            [
                "az",
                "vm",
                "image",
                "list",
                "--publisher",
                publisher,
                "--all",
                "--output",
                "json",
            ]
        )
        return ImageInfo.from_az_image_list(json.loads(result.stdout))

    # --------------------------------------------------------------- purge

    def purge(self) -> None:
        """Destroy all VM workspaces via ``tofu destroy``.

        The ``.shared/`` workspace is preserved.
        """
        for item in sorted(self._work_dir.iterdir()):
            if item.name.startswith("."):
                continue
            if not item.is_dir():
                continue
            if not (item / "main.tf").exists():
                continue
            self._run(
                ["tofu", "destroy", "-auto-approve"], cwd=str(item)
            )

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
        """Ensure the named VM exists and is running.

        State machine:
        - Workspace missing  : launch with provided parameters
        - ``AzureVmCommandError``,
          ``JSONDecodeError``: re-launch
        - Running            : no-op
        - Any other          : start (Stopped, etc.)

        Returns the :class:`AzureVM` instance in all cases.
        """
        workspace = self._work_dir / name

        if not workspace.exists() or not (workspace / "main.tf").exists():
            return self.launch(
                name=name,
                vm_size=vm_size,
                disk_size_gb=disk_size_gb,
                image_urn=image_urn,
                cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path,
            )

        vm = self.get_vm(name)
        try:
            info = vm.info()
        except (AzureVmCommandError, json.JSONDecodeError):
            return self.launch(
                name=name,
                vm_size=vm_size,
                disk_size_gb=disk_size_gb,
                image_urn=image_urn,
                cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path,
            )

        if info.state == VmState.RUNNING:
            return vm

        vm.start()
        return self.get_vm(name)
