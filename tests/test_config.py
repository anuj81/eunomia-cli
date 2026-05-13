"""CLI config — defaults + TOML overrides."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from src import config as cfg_mod


def test_defaults_when_file_missing(_eunomia_cli_home):
    # tmp dir is fresh — no config file yet
    cfg = cfg_mod.load_config()
    assert cfg.keycloak_url == "http://localhost:8080"
    assert cfg.realm == "eunomia"
    assert cfg.client_id == "eunomia-cli"
    assert cfg.middleware_url == "http://localhost:8000"


def test_toml_override(_eunomia_cli_home):
    cfg_mod.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg_mod.CONFIG_FILE.write_text(
        """
[keycloak]
url = "https://kc.prod.internal"
realm = "production"
client_id = "eunomia-cli-prod"

[middleware]
url = "https://api.eunomia.example/v1"
"""
    )
    cfg = cfg_mod.load_config()
    assert cfg.keycloak_url == "https://kc.prod.internal"
    assert cfg.realm == "production"
    assert cfg.client_id == "eunomia-cli-prod"
    assert cfg.middleware_url == "https://api.eunomia.example/v1"


def test_endpoint_helpers_resolve():
    cfg = cfg_mod.CliConfig(
        keycloak_url="http://example/",   # trailing slash to confirm normalization
        realm="r",
        client_id="c",
        middleware_url="http://m",
    )
    assert cfg.issuer == "http://example/realms/r"
    assert cfg.device_endpoint.endswith("/protocol/openid-connect/auth/device")
    assert cfg.token_endpoint.endswith("/protocol/openid-connect/token")
    assert cfg.jwks_endpoint.endswith("/protocol/openid-connect/certs")


def test_partial_toml_uses_defaults_for_missing_fields(_eunomia_cli_home):
    cfg_mod.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg_mod.CONFIG_FILE.write_text(
        """
[keycloak]
realm = "staging"
"""
    )
    cfg = cfg_mod.load_config()
    assert cfg.realm == "staging"
    # Other fields stayed at defaults
    assert cfg.keycloak_url == "http://localhost:8080"
    assert cfg.middleware_url == "http://localhost:8000"
