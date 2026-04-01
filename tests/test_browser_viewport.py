"""headed_browser_viewport() env parsing."""

from __future__ import annotations

import pytest

import uber_eats_mcp.browser as browser


def test_headed_browser_viewport_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UBEREATS_BROWSER_VIEWPORT", raising=False)
    assert browser.headed_browser_viewport() == (1280, 900)


def test_headed_browser_viewport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UBEREATS_BROWSER_VIEWPORT", "1024x768")
    assert browser.headed_browser_viewport() == (1024, 768)


def test_headed_browser_viewport_star_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UBEREATS_BROWSER_VIEWPORT", "900*700")
    assert browser.headed_browser_viewport() == (900, 700)
