"""Unit tests for login detection helpers (no real browser, no Uber network)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import uber_eats_mcp.ubereats as ubereats
from uber_eats_mcp.browser import UberEatsConfig, manager


@pytest.fixture
def fresh_config() -> UberEatsConfig:
    """Avoid mutating whatever the singleton had when tests started."""
    prev = manager.config
    manager.config = UberEatsConfig()
    yield manager.config
    manager.config = prev


def test_sync_session_hints_sets_sid_ubereats_domain(fresh_config: UberEatsConfig) -> None:
    page = MagicMock()
    page.context.cookies = AsyncMock(
        return_value=[
            {"domain": ".ubereats.com", "name": "sid", "value": "session-from-jar"},
            {"domain": ".ubereats.com", "name": "csrftoken", "value": "csrf-val"},
        ]
    )

    asyncio.run(ubereats._sync_session_hints_from_browser(page))
    assert fresh_config.sid == "session-from-jar"
    assert fresh_config.cookies["sid"] == "session-from-jar"
    assert fresh_config.cookies["csrftoken"] == "csrf-val"
    assert ubereats._has_auth_signals() is True


def test_sync_session_hints_sets_sid_uber_domain(fresh_config: UberEatsConfig) -> None:
    page = MagicMock()
    page.context.cookies = AsyncMock(
        return_value=[{"domain": "auth.uber.com", "name": "sid", "value": "uber-auth-sid"}]
    )
    asyncio.run(ubereats._sync_session_hints_from_browser(page))
    assert fresh_config.sid == "uber-auth-sid"
    assert ubereats._has_auth_signals() is True


def test_sync_session_hints_ignores_foreign_domains(fresh_config: UberEatsConfig) -> None:
    page = MagicMock()
    page.context.cookies = AsyncMock(
        return_value=[{"domain": "google.com", "name": "sid", "value": "not-used"}]
    )
    asyncio.run(ubereats._sync_session_hints_from_browser(page))
    assert fresh_config.sid == ""
    assert not fresh_config.cookies
    assert ubereats._has_auth_signals() is False


def test_has_auth_signals_user_id_without_sid(fresh_config: UberEatsConfig) -> None:
    fresh_config.user_id = "u-1"
    assert ubereats._has_auth_signals() is True


def test_check_login_dom_no_sign_in_links() -> None:
    page = MagicMock()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.get_by_role.return_value = loc
    assert asyncio.run(ubereats._check_login_dom(page)) is True


def test_check_login_dom_sign_in_still_visible() -> None:
    page = MagicMock()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)
    page.get_by_role.return_value = loc
    assert asyncio.run(ubereats._check_login_dom(page)) is False
