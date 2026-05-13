"""Per-test env hygiene — point the CLI's home at a tmp dir for each test."""

import importlib
import pytest


@pytest.fixture(autouse=True)
def _eunomia_cli_home(tmp_path, monkeypatch):
    """Redirect ~/.eunomia to a tmp dir so tests never touch the developer's
    actual login cache."""
    monkeypatch.setenv("EUNOMIA_CLI_HOME", str(tmp_path))
    # Force module re-evaluation so module-level paths (CONFIG_DIR, TOKEN_FILE)
    # pick up the new env var.
    from src import config as _cfg
    importlib.reload(_cfg)
    from src import auth as _auth
    importlib.reload(_auth)
    yield tmp_path
