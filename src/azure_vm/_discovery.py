"""Azure Marketplace image discovery."""

from __future__ import annotations

import json

from ._backend import CommandBackend, run_command
from .models import ImageInfo


def list_images(
    backend: CommandBackend,
    publisher: str = "Canonical",
) -> list[ImageInfo]:
    """List available VM images from Azure Marketplace."""
    result = run_command(
        backend,
        [
            "az", "vm", "image", "list",
            "--publisher", publisher,
            "--all",
            "--output", "json",
        ],
    )
    return ImageInfo.from_az_image_list(json.loads(result.stdout))
