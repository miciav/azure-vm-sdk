"""End-to-end verification: create a VM, verify readiness, execute a command,
delete the VM.

Prerequisites:
    az login
    tofu installed on PATH
    AZURE_RESOURCE_GROUP and AZURE_LOCATION env vars set

Usage:
    uv run azure-vm-e2e
    uv run azure-vm-e2e --name my-test-vm --vm-size Standard_D2s_v3 --timeout 300
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .client import AzureClient
from .exceptions import AzureVmError


def _env_or_raise(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="End-to-end VM lifecycle test (create, verify, delete)."
    )
    parser.add_argument(
        "--name", default=None,
        help="VM name (auto-generated if not provided).",
    )
    parser.add_argument(
        "--vm-size", default="Standard_B1s",
        help="Azure VM size (default: Standard_B1s).",
    )
    parser.add_argument(
        "--image-urn", default=None,
        help="Azure image URN (publisher:offer:sku:version).",
    )
    parser.add_argument(
        "--timeout", type=float, default=300,
        help="Max seconds to wait for SSH readiness (default: 300).",
    )
    args = parser.parse_args()

    # --- configuration ---------------------------------------------------
    resource_group = _env_or_raise("AZURE_RESOURCE_GROUP")
    location = _env_or_raise("AZURE_LOCATION")
    ssh_key_path = os.environ.get("AZURE_SSH_PUBLIC_KEY")

    client = AzureClient(
        resource_group=resource_group,
        location=location,
        ssh_key_path=ssh_key_path,
    )

    name = args.name or f"e2e-{int(time.time())}"
    vm = None
    exit_code = 0

    try:
        # --- create ------------------------------------------------------
        print(f"[1/5] Launching VM '{name}' (size={args.vm_size}) ...")
        t0 = time.monotonic()
        vm = client.launch(
            name=name,
            vm_size=args.vm_size,
            image_urn=args.image_urn,
        )
        dt = time.monotonic() - t0
        print(f"       launch completed in {dt:.1f}s")

        # --- wait for ip -------------------------------------------------
        print(f"[2/5] Waiting for public IP (timeout={args.timeout}s) ...")
        t0 = time.monotonic()
        ip = vm.wait_for_ip(timeout=args.timeout)
        dt = time.monotonic() - t0
        print(f"       got IP {ip} in {dt:.1f}s")

        # --- wait for ssh ------------------------------------------------
        print(f"[3/5] Waiting for SSH on {ip}:22 ...")
        t0 = time.monotonic()
        ready_ip = vm.wait_ready(timeout=args.timeout, port=22)
        dt = time.monotonic() - t0
        print(f"       SSH ready on {ready_ip} in {dt:.1f}s")

        # --- verify ------------------------------------------------------
        print("[4/5] Running verification command ...")
        result = vm.exec(["uname", "-a"])
        print(f"       exit={result.returncode}  stdout={result.stdout.strip()}")
        if result.stderr:
            print(f"       stderr={result.stderr.strip()}")
        if not result.success:
            print("       FAILED: verification command returned non-zero")
            exit_code = 1

        info = vm.info()
        print(f"       state={info.state.value}  location={info.location}  "
              f"resource_group={info.resource_group}")

    except AzureVmError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        exit_code = 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130

    # --- cleanup ---------------------------------------------------------
    print(f"[5/5] Deleting VM '{name}' ...")
    try:
        if vm is not None:
            vm.delete()
            print("       done.")
    except AzureVmError as exc:
        print(f"       cleanup failed: {exc}", file=sys.stderr)
        exit_code = 3

    print()
    if exit_code == 0:
        print(f"SUCCESS — VM '{name}' completed full lifecycle.")
    else:
        print(f"FAILURE (exit code {exit_code}) — see errors above.")

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
