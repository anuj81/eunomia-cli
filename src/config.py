"""CLI configuration — defaults + optional TOML override at ~/.eunomia/config.toml.

The CLI is a thin client and doesn't need a heavy settings stack like the
middleware. We load a TOML file if present, otherwise fall back to dev
defaults. No secrets here — the device-code flow doesn't need a client
secret (eunomia-cli is configured as a public client in Keycloak).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:                                                   # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


CONFIG_DIR = Path(os.environ.get("EUNOMIA_CLI_HOME") or (Path.home() / ".eunomia"))
CONFIG_FILE = CONFIG_DIR / "config.toml"
TOKEN_FILE = CONFIG_DIR / "cli.json"


@dataclass(frozen=True)
class CliConfig:
    keycloak_url:    str       # e.g. "http://localhost:8080"
    realm:           str       # e.g. "eunomia"
    client_id:       str       # e.g. "eunomia-cli"
    middleware_url:  str       # e.g. "http://localhost:8000"

    @property
    def issuer(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.realm}"

    @property
    def device_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/auth/device"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/token"

    @property
    def jwks_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/certs"


_DEFAULTS = CliConfig(
    keycloak_url="http://localhost:8080",
    realm="eunomia",
    client_id="eunomia-cli",
    middleware_url="http://localhost:8000",
)


def load_config() -> CliConfig:
    """Read ~/.eunomia/config.toml if present; fall back to dev defaults.

    A missing file is not an error — fresh installs get the dev defaults and
    can override piecemeal by writing the TOML.
    """
    if not CONFIG_FILE.exists():
        return _DEFAULTS

    with CONFIG_FILE.open("rb") as f:
        data = tomllib.load(f)

    kc = data.get("keycloak", {}) or {}
    mw = data.get("middleware", {}) or {}
    return CliConfig(
        keycloak_url=str(kc.get("url",       _DEFAULTS.keycloak_url)),
        realm=str(kc.get("realm",            _DEFAULTS.realm)),
        client_id=str(kc.get("client_id",    _DEFAULTS.client_id)),
        middleware_url=str(mw.get("url",     _DEFAULTS.middleware_url)),
    )
