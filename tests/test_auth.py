"""CLI auth — device-code flow, token cache, refresh, decode."""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from pathlib import Path
from typing import Dict, List

import httpx
import pytest

from src import auth
from src.config import CliConfig


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


CFG = CliConfig(
    keycloak_url="http://kc.test",
    realm="eunomia",
    client_id="eunomia-cli",
    middleware_url="http://mw.test",
)


def _fake_jwt(payload: dict) -> str:
    """Produce a structurally-valid (signature-unverified) JWT for tests."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig-placeholder"


# --------------------------------------------------------------------------- #
# Token cache                                                                 #
# --------------------------------------------------------------------------- #


def test_save_and_load_tokens_roundtrip(_eunomia_cli_home):
    b = auth.TokenBundle(
        access_token="at", refresh_token="rt",
        expires_at=time.time() + 60,
        refresh_expires_at=time.time() + 3600,
        issuer=CFG.issuer,
    )
    auth.save_tokens(b)
    got = auth.load_tokens()
    assert got is not None
    assert got.access_token == "at"
    assert got.refresh_token == "rt"
    assert got.issuer == CFG.issuer


def test_save_tokens_writes_mode_0600(_eunomia_cli_home):
    b = auth.TokenBundle(access_token="x", refresh_token=None,
                          expires_at=time.time()+60, refresh_expires_at=None,
                          issuer=CFG.issuer)
    auth.save_tokens(b)
    mode = stat.S_IMODE(os.stat(auth.TOKEN_FILE).st_mode)
    # Allow group bits to be 0; primary check is no world-readable.
    assert mode & 0o077 == 0, f"token file is too permissive: {oct(mode)}"


def test_load_tokens_returns_none_when_missing(_eunomia_cli_home):
    assert auth.load_tokens() is None


def test_load_tokens_returns_none_on_corrupt_file(_eunomia_cli_home):
    auth.TOKEN_FILE.parent.mkdir(exist_ok=True)
    auth.TOKEN_FILE.write_text("{not valid json")
    assert auth.load_tokens() is None


def test_clear_tokens_returns_true_when_file_existed(_eunomia_cli_home):
    auth.save_tokens(auth.TokenBundle(
        access_token="a", refresh_token="r",
        expires_at=time.time()+60, refresh_expires_at=None, issuer="x",
    ))
    assert auth.clear_tokens() is True
    assert auth.load_tokens() is None


def test_clear_tokens_returns_false_when_no_file(_eunomia_cli_home):
    assert auth.clear_tokens() is False


# --------------------------------------------------------------------------- #
# decode_claims                                                               #
# --------------------------------------------------------------------------- #


def test_decode_claims_extracts_payload():
    token = _fake_jwt({"sub": "u", "email": "u@x.com", "exp": 12345,
                        "realm_access": {"roles": ["eunomia-finance-user"]}})
    claims = auth.decode_claims(token)
    assert claims["sub"] == "u"
    assert claims["email"] == "u@x.com"
    assert claims["realm_access"]["roles"] == ["eunomia-finance-user"]


def test_decode_claims_returns_empty_for_garbage():
    assert auth.decode_claims("not-a-jwt") == {}
    assert auth.decode_claims("") == {}


# --------------------------------------------------------------------------- #
# Device flow                                                                 #
# --------------------------------------------------------------------------- #


def test_begin_device_flow_happy_path(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=CFG.device_endpoint,
        json={
            "device_code": "abc-xyz",
            "user_code":   "ABCD-EFGH",
            "verification_uri":          "http://kc.test/realms/eunomia/device",
            "verification_uri_complete": "http://kc.test/realms/eunomia/device?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval":   3,
        },
    )
    start = auth.begin_device_flow(CFG)
    assert start.device_code == "abc-xyz"
    assert start.user_code == "ABCD-EFGH"
    assert start.expires_in == 600
    assert start.interval == 3


def test_begin_device_flow_server_error_raises(httpx_mock):
    httpx_mock.add_response(method="POST", url=CFG.device_endpoint,
                             status_code=500, text="boom")
    with pytest.raises(auth.AuthError):
        auth.begin_device_flow(CFG)


# --------------------------------------------------------------------------- #
# poll_device_token                                                           #
# --------------------------------------------------------------------------- #


def _start_payload() -> auth.DeviceCodeStart:
    return auth.DeviceCodeStart(
        device_code="dev-code",
        user_code="USER-CODE",
        verification_uri="http://kc.test/realms/eunomia/device",
        verification_uri_complete=None,
        expires_in=600,
        interval=1,
    )


def test_poll_device_token_succeeds_after_pending(httpx_mock):
    # First poll: pending
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint,
                             status_code=400, json={"error": "authorization_pending"})
    # Second poll: success
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint, json={
        "access_token": "at-1", "refresh_token": "rt-1",
        "expires_in": 60, "refresh_expires_in": 3600,
    })

    sleeps: List[float] = []
    bundle = auth.poll_device_token(CFG, _start_payload(),
                                     sleep_fn=lambda s: sleeps.append(s),
                                     now_fn=lambda: 1000.0)
    assert bundle.access_token == "at-1"
    assert bundle.refresh_token == "rt-1"
    assert sleeps == [1]   # waited the interval once before second poll


def test_poll_device_token_slow_down_increases_interval(httpx_mock):
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint,
                             status_code=400, json={"error": "slow_down"})
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint, json={
        "access_token": "at", "expires_in": 60,
    })
    sleeps: List[float] = []
    auth.poll_device_token(CFG, _start_payload(),
                            sleep_fn=lambda s: sleeps.append(s),
                            now_fn=lambda: 1000.0)
    # slow_down bumps interval by 5 → second wait should be 6 (1 + 5)
    assert sleeps == [6]


def test_poll_device_token_access_denied_aborts(httpx_mock):
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint,
                             status_code=400, json={"error": "access_denied"})
    with pytest.raises(auth.DeviceFlowAborted):
        auth.poll_device_token(CFG, _start_payload(),
                                sleep_fn=lambda s: None,
                                now_fn=lambda: 1000.0)


def test_poll_device_token_deadline_expires():
    """If now_fn advances past start.expires_in, raise — no HTTP probed."""
    # First call (when computing deadline): t=1000. Second call (loop guard
    # at the top of the first iteration): t=100_000 → past the 1000+600 deadline.
    times = iter([1000.0, 100_000.0])
    # No httpx mocks → if a request is attempted, pytest-httpx will fail it.
    with pytest.raises(auth.DeviceFlowAborted):
        auth.poll_device_token(CFG, _start_payload(),
                                sleep_fn=lambda s: None,
                                now_fn=lambda: next(times))


# --------------------------------------------------------------------------- #
# refresh_tokens                                                              #
# --------------------------------------------------------------------------- #


def test_refresh_tokens_happy_path(httpx_mock):
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint, json={
        "access_token": "new-at", "refresh_token": "new-rt",
        "expires_in": 60, "refresh_expires_in": 3600,
    })
    old = auth.TokenBundle(
        access_token="old", refresh_token="old-rt",
        expires_at=time.time(), refresh_expires_at=time.time()+10,
        issuer=CFG.issuer,
    )
    new = auth.refresh_tokens(CFG, old)
    assert new.access_token == "new-at"
    assert new.refresh_token == "new-rt"


def test_refresh_tokens_raises_without_refresh_token():
    b = auth.TokenBundle(access_token="x", refresh_token=None,
                          expires_at=time.time(), refresh_expires_at=None,
                          issuer=CFG.issuer)
    with pytest.raises(auth.AuthError):
        auth.refresh_tokens(CFG, b)


def test_refresh_tokens_keycloak_failure_raises(httpx_mock):
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint,
                             status_code=400, json={"error": "invalid_grant"})
    b = auth.TokenBundle(access_token="x", refresh_token="r",
                          expires_at=time.time(), refresh_expires_at=time.time()+10,
                          issuer=CFG.issuer)
    with pytest.raises(auth.AuthError):
        auth.refresh_tokens(CFG, b)


# --------------------------------------------------------------------------- #
# access_token (the public accessor)                                          #
# --------------------------------------------------------------------------- #


def test_access_token_returns_cached_when_fresh(_eunomia_cli_home):
    auth.save_tokens(auth.TokenBundle(
        access_token="cached-at", refresh_token="rt",
        expires_at=time.time() + 300, refresh_expires_at=time.time()+3600,
        issuer=CFG.issuer,
    ))
    assert auth.access_token(CFG) == "cached-at"


def test_access_token_auto_refreshes_when_near_expiry(_eunomia_cli_home, httpx_mock):
    auth.save_tokens(auth.TokenBundle(
        access_token="stale", refresh_token="r",
        expires_at=time.time() + 5,           # < refresh window
        refresh_expires_at=time.time() + 3600,
        issuer=CFG.issuer,
    ))
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint, json={
        "access_token": "fresh-at", "refresh_token": "fresh-rt",
        "expires_in": 60, "refresh_expires_in": 3600,
    })
    got = auth.access_token(CFG)
    assert got == "fresh-at"
    # Cache updated
    cached = auth.load_tokens()
    assert cached.access_token == "fresh-at"


def test_access_token_raises_when_not_logged_in(_eunomia_cli_home):
    with pytest.raises(auth.NotLoggedInError):
        auth.access_token(CFG)


def test_access_token_refresh_failure_surfaces_not_logged_in(_eunomia_cli_home, httpx_mock):
    auth.save_tokens(auth.TokenBundle(
        access_token="expired", refresh_token="r",
        expires_at=time.time() - 100, refresh_expires_at=time.time()+10,
        issuer=CFG.issuer,
    ))
    httpx_mock.add_response(method="POST", url=CFG.token_endpoint,
                             status_code=400, json={"error": "invalid_grant"})
    with pytest.raises(auth.NotLoggedInError):
        auth.access_token(CFG)


def test_cached_identity_returns_decoded(_eunomia_cli_home):
    token = _fake_jwt({"sub": "u", "preferred_username": "alice"})
    auth.save_tokens(auth.TokenBundle(
        access_token=token, refresh_token=None,
        expires_at=time.time()+60, refresh_expires_at=None, issuer=CFG.issuer,
    ))
    claims = auth.cached_identity()
    assert claims is not None
    assert claims["preferred_username"] == "alice"


def test_cached_identity_returns_none_when_logged_out(_eunomia_cli_home):
    assert auth.cached_identity() is None
