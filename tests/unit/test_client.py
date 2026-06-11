import json
from pathlib import Path
import pytest
from azure_vm._backend import CommandResult, FakeBackend
from azure_vm.client import AzureClient
from azure_vm.exceptions import AzureVmCommandError, VmNotFoundError
from azure_vm.models import VmState
from azure_vm.vm import AzureVM


OUTPUT_JSON = json.dumps({
    "vm_ip": {"value": "1.2.3.4"},
    "vm_state": {"value": "running"},
    "location": {"value": "westeurope"},
    "vm_size": {"value": "Standard_B1s"},
    "image_urn": {"value": "Canonical:ubuntu-24_04-lts:server-gen1:latest"},
    "resource_group": {"value": "my-rg"},
})


def make_ok(stdout: str = "") -> CommandResult:
    return CommandResult(args=[], returncode=0, stdout=stdout, stderr="")


def make_err(stderr: str = "error") -> CommandResult:
    return CommandResult(args=[], returncode=1, stdout="", stderr=stderr)


# ------------------------------------------------------------ get_vm

def test_get_vm_returns_azure_vm(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    vm_ws = ws / "my-vm"
    vm_ws.mkdir()
    (vm_ws / "main.tf").write_text("")
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=FakeBackend(),
    )
    vm = client.get_vm("my-vm")
    assert isinstance(vm, AzureVM)
    assert vm.name == "my-vm"


def test_get_vm_raises_when_workspace_missing(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=FakeBackend(),
    )
    with pytest.raises(VmNotFoundError) as exc_info:
        client.get_vm("nonexistent")
    assert exc_info.value.name == "nonexistent"


# ------------------------------------------------------------ launch

def test_launch_creates_workspace_and_runs_tofu(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.launch(name="test-vm")
    assert vm.name == "test-vm"
    assert (ws / "test-vm").is_dir()
    assert (ws / "test-vm" / "main.tf").exists()
    calls = backend.calls
    assert any(call[0] == "tofu" and call[1] == "init" for call in calls)
    assert any(call[0] == "tofu" and call[1] == "apply" for call in calls)


def test_launch_shared_infra_created_on_first_call(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="vm1")
    assert (ws / ".shared" / "main.tf").exists()
    shared_init = [
        call for call, cwd in zip(backend.calls, backend.cwds)
        if "init" in call and cwd and ".shared" in cwd
    ]
    assert len(shared_init) == 1


def test_launch_shared_infra_skipped_on_second_call(tmp_path):
    # Shared infra is re-applied only when its rendered config CHANGES:
    # two launches with the same config must apply it exactly once.
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="vm2")
    client.launch(name="vm3")
    shared_apply = [
        call for call, cwd in zip(backend.calls, backend.cwds)
        if "apply" in call and cwd and ".shared" in cwd
    ]
    assert len(shared_apply) == 1


def test_launch_generates_random_name_when_none():
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    vm = client.launch()
    assert vm.name
    assert len(vm.name) > 0


def test_launch_with_cloud_init_config_dict(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(
        name="test-vm",
        cloud_init_config={"packages": ["git", "curl"]},
    )
    workspace = ws / "test-vm"
    main = workspace / "main.tf"
    content = main.read_text()
    assert "custom_data" in content
    cloud_init = workspace / "cloud-init.yaml"
    assert cloud_init.exists()
    cloud_content = cloud_init.read_text()
    assert "git" in cloud_content
    assert "#cloud-config" in cloud_content


def test_launch_with_vm_size_and_disk(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="test-vm", vm_size="Standard_D2s_v3", disk_size_gb=100)
    tfvars = (ws / "test-vm" / "terraform.tfvars").read_text()
    assert 'Standard_D2s_v3' in tfvars
    assert '100' in tfvars


def test_launch_raises_on_failure():
    backend = FakeBackend()
    backend.set_default(make_err("launch failed"))
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    with pytest.raises(AzureVmCommandError):
        client.launch(name="bad-vm")


def test_launch_raises_when_missing_rg():
    backend = FakeBackend()
    client = AzureClient(work_dir="/tmp/ws", backend=backend)
    with pytest.raises(AzureVmCommandError):
        client.launch(name="vm")


# --------------------------------------------------------------- list

def test_list_returns_vms_from_workspace(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    vm_ws = ws / "vm-a"
    vm_ws.mkdir()
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend({
        ("tofu", "output", "-json"): make_ok(OUTPUT_JSON),
    })
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vms = client.list()
    assert len(vms) == 1
    assert vms[0].name == "vm-a"
    assert vms[0].state == VmState.RUNNING


def test_list_skips_shared_and_non_dirs(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    (ws / "readme.txt").write_text("not a vm")
    vm_ws = ws / "real-vm"
    vm_ws.mkdir()
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend({
        ("tofu", "output", "-json"): make_ok(OUTPUT_JSON),
    })
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vms = client.list()
    assert len(vms) == 1
    assert vms[0].name == "real-vm"


# --------------------------------------------------------------- find

AZ_IMAGE_LIST = json.dumps([
    {"offer": "ubuntu-24_04-lts", "publisher": "Canonical",
     "sku": "server-gen1", "version": "latest"},
])


def test_find_returns_image_list():
    backend = FakeBackend({
        ("az", "vm", "image", "list", "--publisher", "Canonical",
         "--all", "--output", "json"): make_ok(AZ_IMAGE_LIST),
    })
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    images = client.find()
    assert len(images) == 1
    assert images[0].sku == "server-gen1"


def test_find_with_custom_publisher():
    backend = FakeBackend({
        ("az", "vm", "image", "list", "--publisher", "Debian",
         "--all", "--output", "json"): make_ok(AZ_IMAGE_LIST),
    })
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    images = client.find(publisher="Debian")
    assert len(images) == 1


# -------------------------------------------------------------- purge

def test_purge_destroys_all_vm_workspaces(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    (ws / "vm-a").mkdir()
    (ws / "vm-a" / "main.tf").write_text("")
    (ws / "vm-b").mkdir()
    (ws / "vm-b" / "main.tf").write_text("")
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.purge()
    destroy_calls = [c for c in backend.calls if c[1] == "destroy"]
    assert len(destroy_calls) == 2
    destroy_cwds = [cwd for cwd in backend.cwds if cwd and "vm-" in cwd]
    assert len(destroy_cwds) == 2


def test_purge_preserves_shared(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.purge()
    shared_destroy = [
        call for call, cwd in zip(backend.calls, backend.cwds)
        if "destroy" in call and cwd and ".shared" in cwd
    ]
    assert len(shared_destroy) == 0


# ---------------------------------------------------- ensure_running

def test_ensure_running_launches_when_not_found(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.ensure_running("new-vm")
    assert vm.name == "new-vm"
    assert any(call[1] == "apply" for call in backend.calls)


def test_ensure_running_is_noop_when_running(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    vm_ws = ws / "my-vm"
    vm_ws.mkdir(parents=True)
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend({
        ("tofu", "output", "-json"): make_ok(OUTPUT_JSON),
    })
    backend.set_default(make_ok())  # shared-infra re-render may init/apply once
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.ensure_running("my-vm")
    assert vm.name == "my-vm"
    # Running VM => no apply in the VM workspace (no start); the workspace
    # files are still re-rendered from the current config.
    vm_applies = [
        call for call, cwd in zip(backend.calls, backend.cwds)
        if "apply" in call and cwd == str(vm_ws)
    ]
    assert vm_applies == []


def test_ensure_running_starts_stopped_vm(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    vm_ws = ws / "my-vm"
    vm_ws.mkdir(parents=True)
    (vm_ws / "main.tf").write_text("")
    stopped = json.dumps({
        "vm_ip": {"value": "1.2.3.4"},
        "vm_state": {"value": "stopped"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {"value": "Canonical:..."},
        "resource_group": {"value": "my-rg"},
    })
    backend = FakeBackend({
        ("tofu", "output", "-json"): make_ok(stopped),
    })
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.ensure_running("my-vm")
    assert vm.name == "my-vm"
    assert any(
        call[1] == "apply" and "desired_state=running" in str(call)
        for call in backend.calls
    )


def test_public_api_importable():
    from azure_vm import (
        AzureClient,
        AzureVM,
        AzureVmError,
        AzureVmCommandError,
        TofuNotInstalledError,
        VmNotFoundError,
        AzureVmTimeoutError,
        SshConnectionError,
        VmInfo,
        VmState,
        ImageInfo,
        CommandResult,
    )
    from azure_vm.testing import FakeBackend

    assert AzureClient is not None
    assert AzureVM is not None
    assert FakeBackend is not None


# ------------------------------------------------------------ launch (cont.)

def test_launch_with_cloud_init_raw_string(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(
        name="test-vm",
        cloud_init_config="#include\nruncmd:\n  - echo hi",
    )
    cloud_init = ws / "test-vm" / "cloud-init.yaml"
    content = cloud_init.read_text()
    assert "#include" in content
    assert "#cloud-config" not in content


def test_launch_with_full_image_urn(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="test-vm", image_urn="Debian:debian-12:12:latest")
    main = (ws / "test-vm" / "main.tf").read_text()
    assert 'publisher = "Debian"' in main
    assert 'offer     = "debian-12"' in main
    assert 'sku       = "12"' in main
    assert 'version   = "latest"' in main


def test_launch_with_partial_image_urn(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="test-vm", image_urn="Debian::bullseye:")
    main = (ws / "test-vm" / "main.tf").read_text()
    assert 'publisher = "Debian"' in main
    assert 'offer     = "ubuntu-24_04-lts"' in main
    assert 'sku       = "bullseye"' in main
    assert 'version   = "latest"' in main


# --------------------------------------------------------------- list (cont.)

def test_list_skips_dirs_without_main_tf(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    dir_without_tf = ws / "empty-dir"
    dir_without_tf.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vms = client.list()
    assert len(vms) == 0


def test_list_handles_json_decode_error(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    vm_ws = ws / "vm-a"
    vm_ws.mkdir()
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend({
        ("tofu", "output", "-json"): make_ok("not-valid-json"),
    })
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vms = client.list()
    assert len(vms) == 0


# -------------------------------------------------------------- purge (cont.)

def test_purge_skips_non_dirs(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    (ws / "readme.txt").write_text("not a vm")
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.purge()
    assert len(backend.calls) == 0


def test_purge_skips_dirs_without_main_tf(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    (ws / "no-tf-dir").mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.purge()
    assert len(backend.calls) == 0


# ---------------------------------------------------- ensure_running (cont.)

def test_ensure_running_relaunch_on_info_error(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    vm_ws = ws / "my-vm"
    vm_ws.mkdir(parents=True)
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend({
        ("tofu", "output", "-json"): make_err("state corrupted"),
    })
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.ensure_running("my-vm")
    assert vm.name == "my-vm"
    assert any(call[1] == "apply" for call in backend.calls)
