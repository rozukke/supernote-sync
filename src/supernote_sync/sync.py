"""One-way, manifest-backed host-to-Supernote folder synchronisation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .client import SupernoteConnection


class SyncError(RuntimeError):
    """A sync configuration, conversion, or transfer error."""


# Derived from the supplied Calibre defaults for fixed 1920×2560 e-ink PDFs.
# Users may replace this list in [calibre].arguments without changing code.
DEFAULT_CALIBRE_ARGUMENTS = (
    "--input-profile", "kindle",
    "--output-profile", "generic_eink_hd",
    "--custom-size", "1920x2560",
    "--unit", "devicepixel",
    "--change-justification", "justify",
    "--minimum-line-height", "133",
    "--pdf-hyphenate",
    "--preserve-cover-aspect-ratio",
    "--pdf-serif-family", "Baskervville",
    "--pdf-sans-family", "Tahoma",
    "--pdf-mono-family", "Courier New",
    "--pdf-standard-font", "serif",
    "--pdf-default-font-size", "25",
    "--pdf-mono-font-size", "25",
    "--pdf-page-numbers",
    "--pdf-add-toc",
    "--pdf-page-margin-left", "110",
    "--pdf-page-margin-top", "90",
    "--pdf-page-margin-right", "100",
    "--pdf-page-margin-bottom", "120",
    "--chapter-mark", "pagebreak",
)


@dataclass(frozen=True, slots=True)
class SyncConfig:
    source_directory: Path
    remote_directory: str
    state_file: Path
    cache_directory: Path
    calibre_command: str
    calibre_arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyncReport:
    converted: int
    uploaded: int
    skipped: int


def load_config(config_file: str | Path) -> SyncConfig:
    """Load a TOML sync configuration without inventing a Calibre profile format."""
    try:
        import tomllib
    except ImportError as error:  # pragma: no cover - Python 3.11+ declares support
        raise SyncError("TOML configuration requires Python 3.11 or newer") from error

    path = Path(config_file).expanduser().resolve()
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError as error:
        raise SyncError(f"configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise SyncError(f"invalid TOML in {path}: {error}") from error
    if not isinstance(raw, dict):
        raise SyncError("configuration must be a TOML table")

    sync = _table(raw, "sync")
    calibre = _table(raw, "calibre")
    source = Path(_string(sync, "source_directory")).expanduser()
    if not source.is_absolute():
        source = path.parent / source
    source = source.resolve()
    if not source.is_dir():
        raise SyncError(f"source_directory is not a directory: {source}")
    remote = _string(sync, "remote_directory")
    state_file = Path(sync.get("state_file", ".supernote-sync-state.json"))
    if not state_file.is_absolute():
        state_file = source / state_file
    cache = Path(sync.get("cache_directory", ".supernote-sync-cache"))
    if not cache.is_absolute():
        cache = source / cache
    arguments = calibre.get("arguments", list(DEFAULT_CALIBRE_ARGUMENTS))
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise SyncError("calibre.arguments must be an array of strings")
    return SyncConfig(
        source_directory=source,
        remote_directory=remote,
        state_file=state_file,
        cache_directory=cache,
        calibre_command=str(calibre.get("command", "ebook-convert")),
        calibre_arguments=tuple(arguments),
    )


def sync_folder(connection: SupernoteConnection, config: SyncConfig, *, dry_run: bool = False) -> SyncReport:
    """Upload new or changed direct children of ``source_directory``.

    EPUBs are converted to cached PDFs by headless Calibre before comparison and
    upload. Existing tablet files are never deleted or overwritten. A local
    SHA-256 manifest prevents repeat uploads; remove the state file to force a
    re-upload.
    """
    state = _load_state(config.state_file)
    files = state.setdefault("files", {})
    converted = uploaded = skipped = 0
    candidates = sorted(path for path in config.source_directory.iterdir() if path.is_file())
    destinations: set[str] = set()
    for source in candidates:
        if source == config.state_file:
            continue
        remote_name, payload, did_convert = _payload_for(source, config, dry_run=dry_run)
        if remote_name in destinations:
            raise SyncError(f"multiple inputs map to the same destination filename: {remote_name}")
        destinations.add(remote_name)
        if did_convert:
            converted += 1
        if dry_run and not payload.is_file():
            print(f"would upload {source.name} -> {config.remote_directory.rstrip('/')}/{remote_name}")
            continue
        digest = _sha256(payload)
        previous = files.get(remote_name)
        if isinstance(previous, dict) and previous.get("sha256") == digest:
            skipped += 1
            continue
        if dry_run:
            print(f"would upload {source.name} -> {config.remote_directory.rstrip('/')}/{remote_name}")
            continue
        result = connection.upload_file(payload, config.remote_directory)
        files[remote_name] = {"sha256": digest, "source": source.name, "remote_path": result.remote_path}
        _save_state(config.state_file, state)
        uploaded += 1
    return SyncReport(converted=converted, uploaded=uploaded, skipped=skipped)


def _payload_for(source: Path, config: SyncConfig, *, dry_run: bool) -> tuple[str, Path, bool]:
    if source.suffix.lower() != ".epub":
        return source.name, source, False
    output = config.cache_directory / f"{source.stem}.pdf"
    # Cache identity includes both the EPUB bytes and exact configured Calibre options.
    metadata = output.with_suffix(".pdf.source.json")
    identity = _sha256(source) + "\0" + "\0".join((config.calibre_command, *config.calibre_arguments))
    known_identity = metadata.read_text() if metadata.is_file() else ""
    if output.is_file() and known_identity == identity:
        return output.name, output, False
    if dry_run:
        print(f"would convert {source.name} -> {output.name}")
        return output.name, output, True
    output.parent.mkdir(parents=True, exist_ok=True)
    _convert_epub(source, output, config)
    metadata.write_text(identity)
    return output.name, output, True


def _convert_epub(source: Path, output: Path, config: SyncConfig) -> None:
    command = shutil.which(config.calibre_command) or config.calibre_command
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".pdf", delete=False) as temporary:
        temp_output = Path(temporary.name)
    try:
        try:
            environment = os.environ.copy()
            fontconfig = _fontconfig_file(output.parent)
            try:
                environment["FONTCONFIG_FILE"] = str(fontconfig)
                subprocess.run(
                    [command, str(source), str(temp_output), *config.calibre_arguments],
                    check=True,
                    env=environment,
                )
            finally:
                fontconfig.unlink(missing_ok=True)
        except FileNotFoundError as error:
            raise SyncError(f"Calibre executable not found: {config.calibre_command}") from error
        except subprocess.CalledProcessError as error:
            raise SyncError(f"Calibre failed converting {source.name} (exit {error.returncode})") from error
        if not temp_output.is_file() or temp_output.stat().st_size == 0:
            raise SyncError(f"Calibre produced no PDF for {source.name}")
        temp_output.replace(output)
    finally:
        temp_output.unlink(missing_ok=True)


def _fontconfig_file(directory: Path) -> Path:
    """Create a short-lived Fontconfig config that exposes our bundled font.

    Calibre's PDF options identify fonts by family name, not a TTF path.  This
    lets ``--pdf-serif-family Baskervville`` work without installing the font
    for the user or changing host-level Fontconfig configuration.
    """
    font_directory = Path(__file__).parent / "assets" / "fonts" / "Baskervville"
    if not font_directory.is_dir():  # An installed package must retain package data.
        raise SyncError(f"bundled Baskervville fonts are missing: {font_directory}")
    with tempfile.NamedTemporaryFile(dir=directory, mode="w", suffix=".conf", delete=False) as file:
        file.write(
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
            '<fontconfig>\n'
            '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
            f"  <dir>{escape(str(font_directory))}</dir>\n"
            "</fontconfig>\n"
        )
        return Path(file.name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "files": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SyncError(f"invalid sync state file {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise SyncError(f"invalid sync state file {path}")
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as temporary:
        json.dump(state, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temp_path = Path(temporary.name)
    temp_path.replace(path)


def _table(value: dict[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    if not isinstance(item, dict):
        raise SyncError(f"configuration requires a [{name}] table")
    return item


def _string(value: dict[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise SyncError(f"{name} must be a non-empty string")
    return item
