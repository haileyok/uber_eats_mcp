"""urls.web_home_url and web_locale_path."""

import uber_eats_mcp.urls as urls

import pytest


def test_web_home_url_default_cl_en(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UBEREATS_WEB_LOCALE", raising=False)
    assert urls.web_locale_path() == "cl-en"
    assert urls.web_home_url() == "https://www.ubereats.com/cl-en/"


def test_web_home_url_us_en(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UBEREATS_WEB_LOCALE", "us-en")
    assert urls.web_locale_path() == "us-en"
    assert urls.web_home_url() == "https://www.ubereats.com/us-en/"


def test_web_home_url_empty_uses_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UBEREATS_WEB_LOCALE", "")
    assert urls.web_locale_path() == ""
    assert urls.web_home_url() == "https://www.ubereats.com/"
