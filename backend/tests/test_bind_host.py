"""Tests for LAN bind-host resolution."""

from __future__ import annotations

from runtime_config.port_constant import advertise_bind_host, resolve_bind_host


def test_resolve_bind_host_defaults_to_localhost() -> None:
    assert resolve_bind_host(None) == "127.0.0.1"
    assert resolve_bind_host("") == "127.0.0.1"
    assert resolve_bind_host("   ") == "127.0.0.1"


def test_resolve_bind_host_accepts_all_interfaces() -> None:
    assert resolve_bind_host("0.0.0.0") == "0.0.0.0"
    assert resolve_bind_host(" 0.0.0.0 ") == "0.0.0.0"


def test_advertise_bind_host_uses_loopback_for_wildcard() -> None:
    assert advertise_bind_host("0.0.0.0") == "127.0.0.1"
    assert advertise_bind_host("::") == "127.0.0.1"
    assert advertise_bind_host("192.168.1.10") == "192.168.1.10"
