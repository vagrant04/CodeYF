from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest


def _is_loopback_address(address: Any) -> bool:
    if not isinstance(address, tuple) or not address:
        return True
    host = str(address[0]).strip("[]")
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression tests may use local HTTP servers but must never call a model provider."""
    real_connect = socket.socket.connect

    def guarded_connect(instance: socket.socket, address: Any) -> Any:
        if not _is_loopback_address(address):
            raise AssertionError(f"external network is forbidden during tests: {address[0]}")
        return real_connect(instance, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
