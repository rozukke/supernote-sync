"""Command-line entry point for the initial discovery and upload workflow."""

from __future__ import annotations

import argparse
from typing import Sequence

from .client import SupernoteConnection, SupernoteError
from .discovery import discover
from .sync import SyncError, load_config, sync_folder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover a Supernote and upload via Browse & Access.")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("discover", help="scan a subnet for Browse & Access")
    scan.add_argument("--subnet", help="IPv4 CIDR to scan (default: local /24)")
    scan.add_argument("--timeout", type=float, default=0.5, help="per-host timeout in seconds")
    scan.add_argument("--port", type=int, default=8089)

    upload = commands.add_parser("upload", help="upload one file")
    upload.add_argument("file")
    upload.add_argument("--host", required=True, help="tablet IP or http://IP:port")
    upload.add_argument("--directory", default="/", help="tablet destination directory")
    upload.add_argument("--timeout", type=float, default=60.0)

    sync = commands.add_parser("sync", help="one-way sync a configured host folder to the tablet")
    sync.add_argument("--config", default="supernote-sync.toml", help="TOML configuration file")
    sync.add_argument("--host", help="tablet IP or http://IP:port; discovered when omitted")
    sync.add_argument("--subnet", help="IPv4 CIDR used when discovering the tablet")
    sync.add_argument("--dry-run", action="store_true", help="show conversions/uploads without changing files or tablet")
    sync.add_argument("--timeout", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "discover":
        devices = discover(args.subnet, port=args.port, timeout=args.timeout)
        for device in devices:
            print(device.base_url)
        return 0 if devices else 1

    if args.command == "upload":
        connection = SupernoteConnection.from_url(args.host, timeout=args.timeout)
        result = connection.upload_file(args.file, args.directory)
        print(f"uploaded {result.bytes_sent} bytes to {result.remote_path} (HTTP {result.status})")
        return 0

    try:
        config = load_config(args.config)
        if args.host:
            connection = SupernoteConnection.from_url(args.host, timeout=args.timeout)
        else:
            devices = discover(args.subnet)
            if not devices:
                raise SyncError("no Supernote discovered; enable Browse & Access or pass --host")
            if len(devices) > 1:
                choices = ", ".join(device.base_url for device in devices)
                raise SyncError(f"multiple Supernotes discovered; pass --host: {choices}")
            connection = SupernoteConnection.from_endpoint(devices[0], timeout=args.timeout)
        report = sync_folder(connection, config, dry_run=args.dry_run)
    except (SyncError, SupernoteError) as error:
        parser.error(str(error))
    print(f"sync complete: {report.uploaded} uploaded, {report.converted} converted, {report.skipped} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
