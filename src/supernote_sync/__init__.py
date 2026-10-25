"""Local-network client primitives for Supernote Browse & Access."""

from .client import SupernoteConnection, UploadResult
from .discovery import DeviceEndpoint, discover, local_subnet
from .sync import SyncConfig, SyncReport, load_config, sync_folder

__all__ = [
    "DeviceEndpoint",
    "SupernoteConnection",
    "UploadResult",
    "discover",
    "local_subnet",
    "SyncConfig",
    "SyncReport",
    "load_config",
    "sync_folder",
]
