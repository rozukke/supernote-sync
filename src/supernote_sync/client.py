"""A small, dependency-free client for Supernote Browse & Access uploads."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import mimetypes
from pathlib import Path
import secrets
from typing import BinaryIO
from urllib.parse import quote, urlparse

from .discovery import DEFAULT_PORT, DeviceEndpoint, probe


class SupernoteError(RuntimeError):
    """Base error for communication with a Supernote."""


class UploadError(SupernoteError):
    """The tablet refused an upload or the transfer failed."""


class RemoteDirectoryError(SupernoteError):
    """The configured Browse & Access destination is not a valid directory."""


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Information about a completed Browse & Access upload."""

    remote_path: str
    bytes_sent: int
    status: int


class SupernoteConnection:
    """Connection to the tablet's Browse & Access service (normally port 8089)."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, *, timeout: float = 20.0) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.host = host
        self.port = port
        self.timeout = timeout

    @classmethod
    def from_url(cls, url: str, *, timeout: float = 20.0) -> "SupernoteConnection":
        """Build a connection from ``http://host:8089`` as shown by the tablet."""
        parsed = urlparse(url if "://" in url else f"http://{url}")
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("expected an HTTP URL such as http://192.168.1.114:8089")
        return cls(parsed.hostname, parsed.port or DEFAULT_PORT, timeout=timeout)

    @classmethod
    def from_endpoint(cls, endpoint: DeviceEndpoint, *, timeout: float = 20.0) -> "SupernoteConnection":
        return cls(endpoint.host, endpoint.port, timeout=timeout)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def probe(self) -> bool:
        """Check that this endpoint currently exposes Browse & Access."""
        return probe(self.host, self.port, min(self.timeout, 5.0))

    def validate_directory(self, directory: str = "/") -> None:
        """Ensure a destination directory exists before starting conversion/upload.

        Browse & Access responds to an unknown path with a redirect to its root.
        Treat that as an error rather than uploading to an unintended location.
        """
        request_path = _directory_path(directory)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("GET", request_path, headers={"Accept": "text/html"})
            response = connection.getresponse()
            response.read(4096)
        except (OSError, http.client.HTTPException) as error:
            raise RemoteDirectoryError(f"could not check tablet directory {request_path}: {error}") from error
        finally:
            connection.close()
        if not 200 <= response.status < 300:
            location = response.getheader("Location")
            redirected = f" (redirected to {location!r})" if location else ""
            raise RemoteDirectoryError(
                f"tablet directory {request_path!r} is unavailable: HTTP {response.status}{redirected}. "
                "Check its spelling and create it on the tablet first."
            )

    def upload_file(self, local_path: str | Path, directory: str = "/") -> UploadResult:
        """Upload a local file into a tablet directory.

        The Browse & Access service accepts a multipart form with a field named
        ``file`` posted to the directory URL.  Content is streamed from disk,
        so uploads do not need to fit in memory.
        """
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(f"not a regular file: {path}")
        with path.open("rb") as source:
            return self._upload_stream(source, path.name, path.stat().st_size, directory)

    def upload_bytes(self, content: bytes, filename: str, directory: str = "/") -> UploadResult:
        """Upload in-memory content; primarily convenient for small generated files."""
        from io import BytesIO

        return self._upload_stream(BytesIO(content), filename, len(content), directory)

    def _upload_stream(self, source: BinaryIO, filename: str, size: int, directory: str) -> UploadResult:
        if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
            raise ValueError("filename must be a basename")
        request_path = _directory_path(directory)
        boundary = f"----supernote-sync-{secrets.token_hex(16)}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        preamble = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{_quoted_filename(filename)}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        closing = f"\r\n--{boundary}--\r\n".encode("ascii")
        total_length = len(preamble) + size + len(closing)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.putrequest("POST", request_path)
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(total_length))
            connection.putheader("Accept", "text/html,application/json")
            connection.endheaders()
            connection.send(preamble)
            while block := source.read(1024 * 1024):
                connection.send(block)
            connection.send(closing)
            response = connection.getresponse()
            response_body = response.read(4096).decode("utf-8", errors="replace").strip()
        except (OSError, http.client.HTTPException) as error:
            raise UploadError(f"upload to {self.base_url}{request_path} failed: {error}") from error
        finally:
            connection.close()
        if not 200 <= response.status < 300:
            detail = f": {response_body[:300]}" if response_body else ""
            location = response.getheader("Location")
            redirected = f" (redirected to {location!r})" if location else ""
            raise UploadError(
                f"tablet returned HTTP {response.status} while uploading {filename!r}{redirected}{detail}"
            )
        remote_path = request_path.rstrip("/") + "/" + filename
        return UploadResult(remote_path=remote_path, bytes_sent=size, status=response.status)


def _directory_path(directory: str) -> str:
    """Convert a user directory to a safely URL-encoded server path."""
    if not directory:
        return "/"
    if "\\" in directory or "\x00" in directory:
        raise ValueError("directory must be a slash-separated tablet path")
    parts = [part for part in directory.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("directory must not contain . or ..")
    # Browse & Access canonicalizes directory URLs with a trailing slash. POSTing
    # the slash-less form receives a 301, and replaying a multipart upload after
    # a redirect risks sending a duplicate file.
    return "/" + "/".join(quote(part, safe="") for part in parts) + "/"


def _quoted_filename(filename: str) -> str:
    # Quoted-string escaping prevents an unusual filename from injecting headers.
    return filename.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
