"""Keycloak OIDC device-code flow + token cache + refresh.

User-facing flow:
    1. ``eunomia-cli login`` calls ``begin_device_flow()``.
    2. The user opens the verification_uri (we print it) and enters the user_code.
    3. CLI polls the token endpoint until success/expiry/decline.
    4. Tokens are persisted to ``~/.eunomia/cli.json`` (mode 0600).

Subsequent commands:
    • ``cached_identity()`` returns the decoded access-token claims (sub,
      preferred_username, roles, exp).
    • ``access_token()`` returns a valid bearer string; if expired/near-expiry
      it auto-refreshes using the refresh_token, persisting the new tokens.
    • ``logout()`` removes the cache file.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import stat
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import httpx

from .config import CONFIG_DIR, TOKEN_FILE, CliConfig


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class AuthError(Exception):
    """Generic auth failure."""


class NotLoggedInError(AuthError):
    """Raised when a command needs a token and the cache is empty."""


class DeviceFlowAborted(AuthError):
    """User cancelled, the code expired, or Keycloak declined."""


# --------------------------------------------------------------------------- #
# Token cache                                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class TokenBundle:
    access_token:  str
    refresh_token: Optional[str]
    expires_at:    float       # absolute unix seconds
    refresh_expires_at: Optional[float]
    issuer:        str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TokenBundle":
        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token"),
            expires_at=float(d["expires_at"]),
            refresh_expires_at=(
                float(d["refresh_expires_at"]) if d.get("refresh_expires_at") else None
            ),
            issuer=d["issuer"],
        )

    def access_expires_in(self) -> float:
        return self.expires_at - time.time()


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def save_tokens(bundle: TokenBundle) -> None:
    _ensure_dir()
    tmp = TOKEN_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(bundle.to_dict(), f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, TOKEN_FILE)


def load_tokens() -> Optional[TokenBundle]:
    if not TOKEN_FILE.exists():
        return None
    try:
        with TOKEN_FILE.open("r", encoding="utf-8") as f:
            return TokenBundle.from_dict(json.load(f))
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def clear_tokens() -> bool:
    """Remove the cache. Returns True if a file was removed."""
    try:
        TOKEN_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


# --------------------------------------------------------------------------- #
# JWT decoding (un-validated — Keycloak signature isn't re-checked client-side)#
# --------------------------------------------------------------------------- #


def decode_claims(token: str) -> Dict[str, Any]:
    """Best-effort base64 decode of the JWT payload. Caller MUST NOT trust
    this for security decisions — Keycloak's signature is what makes the
    token valid. The CLI uses claims for display + expiry checks only."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Device-code flow                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class DeviceCodeStart:
    """The payload Keycloak hands back to begin the flow."""
    device_code:        str
    user_code:          str
    verification_uri:   str
    verification_uri_complete: Optional[str]
    expires_in:         int
    interval:           int


def begin_device_flow(cfg: CliConfig, *, client: Optional[httpx.Client] = None) -> DeviceCodeStart:
    """RFC 8628 device-authorization request. Returns the device-code payload."""
    own_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.post(
            cfg.device_endpoint,
            data={"client_id": cfg.client_id, "scope": "openid profile email"},
        )
        if resp.status_code != 200:
            raise AuthError(f"Device auth start failed: HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        return DeviceCodeStart(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            verification_uri_complete=body.get("verification_uri_complete"),
            expires_in=int(body.get("expires_in", 600)),
            interval=int(body.get("interval", 5)),
        )
    finally:
        if own_client:
            client.close()


def poll_device_token(
    cfg: CliConfig,
    start: DeviceCodeStart,
    *,
    client: Optional[httpx.Client] = None,
    sleep_fn=time.sleep,
    now_fn=time.time,
) -> TokenBundle:
    """Poll the token endpoint until the user approves, the code expires, or
    Keycloak returns a hard error. Caller controls the printable user_code."""
    own_client = client is None
    client = client or httpx.Client(timeout=10.0)
    grant = "urn:ietf:params:oauth:grant-type:device_code"
    deadline = now_fn() + start.expires_in
    interval = max(1, start.interval)
    try:
        while True:
            if now_fn() >= deadline:
                raise DeviceFlowAborted("Device code expired before approval.")
            resp = client.post(
                cfg.token_endpoint,
                data={
                    "client_id":   cfg.client_id,
                    "grant_type":  grant,
                    "device_code": start.device_code,
                },
            )
            if resp.status_code == 200:
                return _tokens_from_response(resp.json(), cfg.issuer)
            err = (resp.json() or {}).get("error", "")
            if err == "authorization_pending":
                sleep_fn(interval)
                continue
            if err == "slow_down":
                interval += 5
                sleep_fn(interval)
                continue
            if err in ("expired_token", "access_denied"):
                raise DeviceFlowAborted(f"Authorization failed: {err}")
            # Any other error is unexpected.
            raise AuthError(f"Token poll failed: HTTP {resp.status_code} {resp.text[:200]}")
    finally:
        if own_client:
            client.close()


def _tokens_from_response(body: Dict[str, Any], issuer: str) -> TokenBundle:
    now = time.time()
    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=now + int(body.get("expires_in", 60)),
        refresh_expires_at=(
            now + int(body["refresh_expires_in"])
            if body.get("refresh_expires_in") else None
        ),
        issuer=issuer,
    )


# --------------------------------------------------------------------------- #
# Refresh                                                                     #
# --------------------------------------------------------------------------- #


def refresh_tokens(cfg: CliConfig, bundle: TokenBundle,
                    *, client: Optional[httpx.Client] = None) -> TokenBundle:
    if not bundle.refresh_token:
        raise AuthError("No refresh_token in cache — log in again.")
    own_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.post(
            cfg.token_endpoint,
            data={
                "client_id":     cfg.client_id,
                "grant_type":    "refresh_token",
                "refresh_token": bundle.refresh_token,
            },
        )
        if resp.status_code != 200:
            raise AuthError(f"Refresh failed: HTTP {resp.status_code} {resp.text[:200]}")
        return _tokens_from_response(resp.json(), cfg.issuer)
    finally:
        if own_client:
            client.close()


# --------------------------------------------------------------------------- #
# Public accessors                                                            #
# --------------------------------------------------------------------------- #


_REFRESH_WINDOW_SECONDS = 30   # refresh if access expires within this window


def access_token(
    cfg: CliConfig,
    *,
    client: Optional[httpx.Client] = None,
    force_refresh: bool = False,
) -> str:
    """Return a valid bearer; auto-refresh + persist on the way out."""
    bundle = load_tokens()
    if bundle is None:
        raise NotLoggedInError(
            "Not logged in. Run `eunomia-cli login` first."
        )
    if force_refresh or bundle.access_expires_in() < _REFRESH_WINDOW_SECONDS:
        try:
            bundle = refresh_tokens(cfg, bundle, client=client)
            save_tokens(bundle)
        except AuthError:
            # Refresh failed — caller should prompt re-login.
            raise NotLoggedInError("Session expired. Run `eunomia-cli login` again.")
    return bundle.access_token


def cached_identity() -> Optional[Dict[str, Any]]:
    """Decoded claims of the currently-cached access token, or None."""
    bundle = load_tokens()
    if bundle is None:
        return None
    return decode_claims(bundle.access_token)
