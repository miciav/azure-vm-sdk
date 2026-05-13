from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VmState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "VmState":
        return cls.UNKNOWN


@dataclass
class VmInfo:
    name: str
    state: VmState
    ipv4: list[str]
    location: str
    vm_size: str
    image_urn: str
    resource_group: str

    @classmethod
    def from_tofu_output(cls, data: dict, name: str) -> "VmInfo":
        ip = data.get("vm_ip", {}).get("value", "")
        state_raw = data.get("vm_state", {}).get("value", "unknown")
        return cls(
            name=name,
            state=VmState(state_raw),
            ipv4=[ip] if ip else [],
            location=data.get("location", {}).get("value", ""),
            vm_size=data.get("vm_size", {}).get("value", ""),
            image_urn=data.get("image_urn", {}).get("value", ""),
            resource_group=data.get("resource_group", {}).get("value", ""),
        )

@dataclass
class ImageInfo:
    publisher: str
    offer: str
    sku: str
    version: str

    @classmethod
    def from_az_image_list(cls, data: list[dict]) -> list["ImageInfo"]:
        return [
            cls(
                publisher=img.get("publisher", ""),
                offer=img.get("offer", ""),
                sku=img.get("sku", ""),
                version=img.get("version", ""),
            )
            for img in data
        ]
