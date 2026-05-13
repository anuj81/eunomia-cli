"""eunomia-cli admin subcommands — role gating, action subcommands, runbooks."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from src import auth
from src.config import CliConfig
from src.main import app


runner = CliRunner()


# --------------------------------------------------------------------------- #
# Helpers — mint a fake JWT and seed the token cache                          #
# --------------------------------------------------------------------------- #


def _fake_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig-placeholder"


def _seed_token(_eunomia_cli_home, roles: list, expires_in: int = 3600) -> None:
    """Plant a token in the cache claiming the given roles."""
    token = _fake_jwt({
        "sub": "test", "preferred_username": "test-user",
        "email": "test@example.org",
        "exp": int(time.time()) + expires_in,
        "realm_access": {"roles": roles},
    })
    auth.save_tokens(auth.TokenBundle(
        access_token=token,
        refresh_token="rt-test",
        expires_at=time.time() + expires_in,
        refresh_expires_at=time.time() + expires_in * 10,
        issuer="http://localhost:8080/realms/eunomia",
    ))


# --------------------------------------------------------------------------- #
# Role gating                                                                 #
# --------------------------------------------------------------------------- #


def test_reindex_rag_refuses_when_not_logged_in(_eunomia_cli_home):
    """No cache → exit code 2, no HTTP."""
    r = runner.invoke(app, ["admin", "reindex-rag", "--rag-api-key", "ignored"])
    assert r.exit_code == 2
    assert "Not logged in" in r.output


def test_reindex_rag_refuses_when_missing_admin_role(_eunomia_cli_home):
    _seed_token(_eunomia_cli_home, roles=["eunomia-finance-user"])
    r = runner.invoke(app, ["admin", "reindex-rag", "--rag-api-key", "ignored"])
    assert r.exit_code == 3
    assert "eunomia-om-admin" in r.output


def test_whoami_om_refuses_without_admin_role(_eunomia_cli_home):
    _seed_token(_eunomia_cli_home, roles=["eunomia-marketing-lead"])
    r = runner.invoke(app, ["admin", "whoami-om"])
    assert r.exit_code == 3


# --------------------------------------------------------------------------- #
# admin reindex-rag — happy path                                              #
# --------------------------------------------------------------------------- #


def test_reindex_rag_happy_path(_eunomia_cli_home, httpx_mock):
    _seed_token(_eunomia_cli_home, roles=["eunomia-om-admin", "eunomia-pii-unmask"])
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:9000/v1/index/refresh",
        json={"fetched": 4, "indexed": 4, "collection": "eunomia_views", "reset": True},
    )
    r = runner.invoke(app, ["admin", "reindex-rag", "--reset",
                              "--rag-api-key", "test-rag-key"])
    assert r.exit_code == 0, r.output
    assert "fetched" in r.output and "4" in r.output
    assert "indexed" in r.output


def test_reindex_rag_passes_reset_flag(_eunomia_cli_home, httpx_mock):
    _seed_token(_eunomia_cli_home, roles=["eunomia-om-admin"])
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:9000/v1/index/refresh",
        json={"fetched": 0, "indexed": 0, "collection": "eunomia_views", "reset": False},
    )
    r = runner.invoke(app, ["admin", "reindex-rag", "--rag-api-key", "k"])
    assert r.exit_code == 0
    # Inspect the captured request
    sent = httpx_mock.get_request()
    body = json.loads(sent.read())
    assert body["reset"] is False
    # Bearer token applied
    assert sent.headers["Authorization"] == "Bearer k"


def test_reindex_rag_handles_rag_error(_eunomia_cli_home, httpx_mock):
    _seed_token(_eunomia_cli_home, roles=["eunomia-om-admin"])
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:9000/v1/index/refresh",
        status_code=500, text="boom",
    )
    r = runner.invoke(app, ["admin", "reindex-rag", "--rag-api-key", "k"])
    assert r.exit_code == 6


def test_reindex_rag_refuses_without_api_key(_eunomia_cli_home, monkeypatch):
    _seed_token(_eunomia_cli_home, roles=["eunomia-om-admin"])
    monkeypatch.delenv("RAG_API_KEY", raising=False)
    r = runner.invoke(app, ["admin", "reindex-rag"])
    assert r.exit_code == 4
    assert "RAG_API_KEY" in r.output


# --------------------------------------------------------------------------- #
# admin whoami-om — happy path                                                #
# --------------------------------------------------------------------------- #


def test_whoami_om_happy_path(_eunomia_cli_home, httpx_mock):
    _seed_token(_eunomia_cli_home, roles=["eunomia-om-admin"])
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8585/api/v1/users/loggedInUser",
        json={
            "name": "om.admin", "email": "om.admin@open-metadata.org",
            "isAdmin": True, "isBot": False, "displayName": "OM Admin",
            "id": "abcd-1234-uuid",
        },
    )
    r = runner.invoke(app, ["admin", "whoami-om"])
    assert r.exit_code == 0
    assert "om.admin" in r.output
    assert "True" in r.output  # isAdmin


def test_whoami_om_handles_om_error(_eunomia_cli_home, httpx_mock):
    _seed_token(_eunomia_cli_home, roles=["eunomia-om-admin"])
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8585/api/v1/users/loggedInUser",
        status_code=403, text="forbidden",
    )
    r = runner.invoke(app, ["admin", "whoami-om"])
    assert r.exit_code == 6


# --------------------------------------------------------------------------- #
# Runbook commands — assert helpful output is printed (no network)            #
# --------------------------------------------------------------------------- #


def test_seed_om_runbook_prints_command():
    r = runner.invoke(app, ["admin", "seed-om"])
    assert r.exit_code == 0
    assert "seed_om_policies.py" in r.output
    assert "eunomia-middleware" in r.output


def test_keycloak_bootstrap_runbook_prints_command():
    r = runner.invoke(app, ["admin", "keycloak-bootstrap"])
    assert r.exit_code == 0
    assert "docker-compose" in r.output
    assert "keycloak" in r.output


def test_verify_runbook_prints_command():
    r = runner.invoke(app, ["admin", "verify"])
    assert r.exit_code == 0
    assert "verify_phase_d.py" in r.output
    assert "eunomia-infrastructure" in r.output


def test_admin_help_lists_all_subcommands():
    r = runner.invoke(app, ["admin", "--help"])
    assert r.exit_code == 0
    for sub in ("reindex-rag", "whoami-om", "seed-om", "keycloak-bootstrap", "verify"):
        assert sub in r.output
