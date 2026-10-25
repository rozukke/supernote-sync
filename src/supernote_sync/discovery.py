"""Discovery for the Supernote Browse & Access HTTP service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import http.client
import ipaddress
import socket
from typing import Iterable

DEFAULT_PORT = 8089


@dataclass(frozen=True, slots=True)
class DeviceEndpoint:
    """A discovered Supernote Browse & Access endpoint."""

    host: str
    port: int = DEFAULT_PORT

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def local_subnet() -> ipaddress.IPv4Network:
    """Return the likely LAN /24 for the route used to reach the Internet.

    Most home networks use a /24.  Pass ``subnet=`` to :func:`discover` when
    yours does not, rather than scanning an unexpectedly large address space.
    No packet is sent by this route lookup.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 80))  # TEST-NET; connect only selects a route.
        address = sock.getsockname()[0]
    return ipaddress.ip_network(f"{address}/24", strict=False)


def _is_browse_and_access_page(body: bytes) -> bool:
    """Recognise the directory page without accepting every host on port 8089."""
    text = body.decode("utf-8", errors="ignore")
    return "const json" in text and "fileList" in text


def probe(host: str, port: int = DEFAULT_PORT, timeout: float = 1.0) -> bool:
    """Return whether *host* appears to run Supernote Browse & Access."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/", headers={"Accept": "text/html"})
        response = connection.getresponse()
        # A listing page is small. A bounded read also keeps a bad responder from
        # consuming unbounded memory during a network scan.
        return response.status == 200 and _is_browse_and_access_page(response.read(128 * 1024))
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def discover(
    subnet: str | ipaddress.IPv4Network | None = None,
    *,
    hosts: Iterable[str] | None = None,
    port: int = DEFAULT_PORT,
    timeout: float = 0.5,
    workers: int = 64,
) -> list[DeviceEndpoint]:
    """Find Supernotes serving Browse & Access on the local network.

    The response is verified as a Supernote listing page; an unrelated process
    merely listening on port 8089 is not reported.  ``hosts`` is useful for a
    targeted scan and takes precedence over ``subnet``.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if hosts is None:
        network = ipaddress.ip_network(subnet) if subnet is not None else local_subnet()
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError("only IPv4 subnets are supported")
        candidates = (str(address) for address in network.hosts())
    else:
        candidates = iter(hosts)

    found: list[DeviceEndpoint] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="supernote-scan") as executor:
        futures = {executor.submit(probe, host, port, timeout): host for host in candidates}
        for future in as_completed(futures):
            host = futures[future]
            try:
                if future.result():
                    found.append(DeviceEndpoint(host, port))
            except OSError:
                # DNS failures and malformed addresses are simply non-matches.
                pass
    return sorted(found, key=lambda endpoint: ipaddress.ip_address(endpoint.host))
