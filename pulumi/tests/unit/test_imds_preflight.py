"""Unit tests for the host-side IMDS preflight in stack_azure."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from stacks.control_plane.azure import ImdsPreflightError, check_uami_attached


_CLIENT_ID = "11111111-1111-1111-1111-111111111111"


class _FakeResponse:
    """Minimal requests.Response stand-in for the bits the preflight reads."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: Any = None,
        text: str = "",
        raise_on_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or (str(json_body) if json_body is not None else "")
        self._raise_on_json = raise_on_json

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("not valid JSON")
        return self._json_body


class _FakeSession:
    """Records the URL/headers passed to ``.get`` and returns a fixed response."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_timeout: float | None = None

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_timeout = timeout
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_preflight_passes_on_valid_token_response() -> None:
    session = _FakeSession(
        _FakeResponse(json_body={"access_token": "eyJ...", "expires_in": "3600"})
    )

    # Returns None on success; the assertion is the absence of raise.
    check_uami_attached(_CLIENT_ID, session=session)  # type: ignore[arg-type]

    # Sanity-check the URL contains the client_id and Metadata header is set.
    assert session.last_url is not None
    assert f"client_id={_CLIENT_ID}" in session.last_url
    assert session.last_headers == {"Metadata": "true"}


def test_preflight_raises_when_imds_unreachable() -> None:
    session = _FakeSession(requests.exceptions.ConnectTimeout("imds down"))

    with pytest.raises(ImdsPreflightError, match="not reachable from this host"):
        check_uami_attached(_CLIENT_ID, session=session)  # type: ignore[arg-type]


def test_preflight_raises_on_identity_not_found_json_error() -> None:
    # IMDS uses HTTP 400 + JSON {error, error_description} for an unknown
    # client_id. The body is the authoritative signal.
    session = _FakeSession(
        _FakeResponse(
            status_code=400,
            json_body={
                "error": "invalid_request",
                "error_description": "Identity not found",
            },
        )
    )

    with pytest.raises(
        ImdsPreflightError, match="IMDS refused to mint a token"
    ):
        check_uami_attached(_CLIENT_ID, session=session)  # type: ignore[arg-type]


def test_preflight_raises_on_non_json_body() -> None:
    session = _FakeSession(
        _FakeResponse(status_code=500, raise_on_json=True, text="<html>...</html>")
    )

    with pytest.raises(ImdsPreflightError, match="non-JSON status=500"):
        check_uami_attached(_CLIENT_ID, session=session)  # type: ignore[arg-type]


def test_preflight_raises_on_200_without_access_token() -> None:
    # IMDS shouldn't do this, but if a future Azure response shape lands
    # without ``access_token`` we want to fail loudly rather than treat
    # the missing field as "all good".
    session = _FakeSession(
        _FakeResponse(json_body={"expires_in": "3600"})
    )

    with pytest.raises(ImdsPreflightError, match="unexpected response status=200"):
        check_uami_attached(_CLIENT_ID, session=session)  # type: ignore[arg-type]
