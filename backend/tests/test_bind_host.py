"""Tests for LAN bind-host resolution."""

from __future__ import annotations

import pytest

from runtime_config.port_constant import (
    advertise_bind_host,
    allowed_origins_for_bind,
    assert_lan_bind_has_auth,
    is_loopback_bind,
    resolve_bind_host,
)


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


def test_is_loopback_bind() -> None:
    assert is_loopback_bind("127.0.0.1") is True
    assert is_loopback_bind("localhost") is True
    assert is_loopback_bind("::1") is True
    assert is_loopback_bind("0.0.0.0") is False
    assert is_loopback_bind("192.168.1.10") is False


def test_assert_lan_bind_has_auth_allows_loopback_without_token() -> None:
    assert_lan_bind_has_auth("127.0.0.1", "", "")
    assert_lan_bind_has_auth("localhost")


def test_assert_lan_bind_has_auth_requires_token_for_wildcard() -> None:
    with pytest.raises(SystemExit, match="LTX_API_TOKEN"):
        assert_lan_bind_has_auth("0.0.0.0", "", "")


def test_assert_lan_bind_has_auth_accepts_api_or_session_token() -> None:
    assert_lan_bind_has_auth("0.0.0.0", "", "lan-secret")
    assert_lan_bind_has_auth("0.0.0.0", "session-secret", "")


def test_allowed_origins_for_bind() -> None:
    loopback = allowed_origins_for_bind("127.0.0.1")
    assert "http://localhost:5173" in loopback
    assert "http://127.0.0.1:5173" in loopback
    assert allowed_origins_for_bind("0.0.0.0") == ["*"]
