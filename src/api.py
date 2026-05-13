"""Middleware client — wraps the /v1/execute_nlq SSE call.

Single responsibility: take a query + cached bearer, return a stream of SSE
events. On 401, refresh the access token once and retry — typical Keycloak
access tokens are short-lived and a refresh between commands is the norm.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterator, Optional

import httpx
from httpx_sse import connect_sse

from . import auth as auth_module
from .config import CliConfig


@dataclass
class StreamEvent:
    event: str       # "status" | "complete" | "error"
    data:  dict


def stream_nlq(cfg: CliConfig, query: str, *, override_token: Optional[str] = None) -> Iterator[StreamEvent]:
    """Yield StreamEvent objects from the middleware's SSE response.

    `override_token` is for non-interactive testing; production CLI calls
    use the cached token via auth.access_token().
    """
    url = f"{cfg.middleware_url.rstrip('/')}/v1/execute_nlq"
    payload = {"query": query}

    token = override_token or auth_module.access_token(cfg)
    for attempt in range(2):   # try once, refresh + retry on 401
        try:
            yield from _stream_with_token(url, payload, token)
            return
        except _Unauthorized:
            if attempt == 1:
                raise
            token = auth_module.access_token(cfg, force_refresh=True)


class _Unauthorized(Exception):
    """Internal — caught by the retry shim."""


def _stream_with_token(url: str, payload: dict, token: str) -> Iterator[StreamEvent]:
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
        with connect_sse(client, "POST", url, headers=headers, json=payload) as event_source:
            # If the very first response is 401, raise before iter_sse() so we
            # can refresh + retry from the top.
            if event_source.response.status_code == 401:
                raise _Unauthorized()
            event_source.response.raise_for_status()
            for sse in event_source.iter_sse():
                try:
                    data = json.loads(sse.data) if sse.data else {}
                except json.JSONDecodeError:
                    data = {"raw": sse.data}
                yield StreamEvent(event=sse.event or "status", data=data)
                if sse.event == "complete":
                    return
