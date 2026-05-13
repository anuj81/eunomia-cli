"""Admin subcommand group — operator-facing actions.

Two kinds of commands:

    Action commands (reindex-rag, whoami-om):
        Issue real HTTP requests. Require the caller to hold the
        `eunomia-om-admin` role in their cached JWT; otherwise refuse
        without touching the network.

    Runbook commands (seed-om, keycloak-bootstrap, verify):
        Print the exact commands an operator should run from the relevant
        repo. These tasks are owned by other repos / docker-compose, and
        we deliberately don't duplicate the logic in the CLI.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import auth
from .config import CliConfig, load_config

console = Console()
app = typer.Typer(help="Admin commands (require eunomia-om-admin).")


# --------------------------------------------------------------------------- #
# Role-gate helper                                                            #
# --------------------------------------------------------------------------- #


_REQUIRED_ROLE = "eunomia-om-admin"


def _require_admin_role() -> str:
    """Return the access token if the caller is admin; raise typer.Exit otherwise."""
    claims = auth.cached_identity()
    if claims is None:
        console.print("[bold red]Not logged in.[/bold red]  Run `eunomia-cli login`.")
        raise typer.Exit(2)
    roles = (claims.get("realm_access") or {}).get("roles") or []
    if _REQUIRED_ROLE not in roles:
        console.print(
            f"[bold red]This command requires the [yellow]{_REQUIRED_ROLE}[/yellow] "
            f"realm role.[/bold red]  Your roles: {roles or '[]'}"
        )
        raise typer.Exit(3)
    # Re-fetch via the auth module (handles auto-refresh on near-expiry).
    cfg = load_config()
    return auth.access_token(cfg)


# --------------------------------------------------------------------------- #
# admin reindex-rag                                                           #
# --------------------------------------------------------------------------- #


@app.command("reindex-rag")
def reindex_rag(
    reset: bool = typer.Option(
        False, "--reset",
        help="Drop the Qdrant collection before re-indexing (full rebuild).",
    ),
    rag_url: Optional[str] = typer.Option(
        None, "--rag-url",
        help="Override RAG service URL (default: http://localhost:9000).",
    ),
    rag_api_key: Optional[str] = typer.Option(
        None, "--rag-api-key",
        envvar="RAG_API_KEY",
        help="Bearer token for the RAG service. Falls back to $RAG_API_KEY.",
    ),
):
    """Trigger the eunomia-rag service to re-pull from OpenMetadata + re-embed."""
    _require_admin_role()
    rag_base = (rag_url or "http://localhost:9000").rstrip("/")
    if not rag_api_key:
        console.print(
            "[bold red]RAG_API_KEY not set.[/bold red]  "
            "Pass --rag-api-key or export RAG_API_KEY."
        )
        raise typer.Exit(4)

    console.print(f"[bold blue]Triggering reindex[/bold blue] {rag_base}/v1/index/refresh "
                  f"({'--reset' if reset else 'upsert'})…")
    try:
        resp = httpx.post(
            f"{rag_base}/v1/index/refresh",
            headers={"Authorization": f"Bearer {rag_api_key}",
                     "Content-Type": "application/json"},
            json={"reset": bool(reset)},
            timeout=httpx.Timeout(120.0),
        )
    except httpx.ConnectError:
        console.print(f"[bold red]Could not reach RAG service at {rag_base}.[/bold red]")
        raise typer.Exit(5)

    if resp.status_code != 200:
        console.print(f"[bold red]RAG service returned HTTP {resp.status_code}[/bold red]: "
                       f"{resp.text[:200]}")
        raise typer.Exit(6)

    body = resp.json()
    t = Table(title="Reindex result", show_header=False, box=None, padding=(0, 1))
    t.add_column(style="cyan"); t.add_column()
    t.add_row("fetched",    str(body.get("fetched", "?")))
    t.add_row("indexed",    str(body.get("indexed", "?")))
    t.add_row("collection", str(body.get("collection", "?")))
    t.add_row("reset",      str(body.get("reset", "?")))
    console.print(t)


# --------------------------------------------------------------------------- #
# admin whoami-om                                                             #
# --------------------------------------------------------------------------- #


@app.command("whoami-om")
def whoami_om():
    """Probe OpenMetadata with the cached JWT — shows what OM thinks the principal is."""
    token = _require_admin_role()
    cfg = load_config()
    # OM is conventionally at the same host as Keycloak/middleware; we expose
    # it via the middleware_url's host root + /api/v1 unless caller overrides.
    # Cleanest is to use the well-known dev address. Operators with a different
    # OM URL should set EUNOMIA_OM_URL in their env.
    import os
    om_base = os.environ.get("EUNOMIA_OM_URL", "http://localhost:8585/api/v1").rstrip("/")
    try:
        resp = httpx.get(f"{om_base}/users/loggedInUser",
                          headers={"Authorization": f"Bearer {token}"},
                          timeout=10.0)
    except httpx.ConnectError:
        console.print(f"[bold red]Could not reach OpenMetadata at {om_base}.[/bold red]")
        raise typer.Exit(5)
    if resp.status_code != 200:
        console.print(f"[bold red]OM returned HTTP {resp.status_code}[/bold red]: "
                       f"{resp.text[:200]}")
        raise typer.Exit(6)
    body = resp.json()
    t = Table(title=f"OM /users/loggedInUser  @  {om_base}",
              show_header=False, box=None, padding=(0, 1))
    t.add_column(style="cyan"); t.add_column()
    t.add_row("name",         str(body.get("name", "-")))
    t.add_row("email",        str(body.get("email", "-")))
    t.add_row("isAdmin",      str(body.get("isAdmin", "-")))
    t.add_row("isBot",        str(body.get("isBot", "-")))
    t.add_row("displayName",  str(body.get("displayName", "-")))
    t.add_row("id",           str(body.get("id", "-"))[:36])
    console.print(t)


# --------------------------------------------------------------------------- #
# admin seed-om       (runbook)                                               #
# --------------------------------------------------------------------------- #


@app.command("seed-om")
def seed_om(
    middleware_dir: str = typer.Option(
        "eunomia-middleware",
        "--middleware-dir",
        help="Path to the eunomia-middleware repo (relative to your shell CWD).",
    ),
):
    """Print the seeding command to run (the script lives in eunomia-middleware)."""
    console.print(Panel.fit(
        Syntax(
            f"cd {middleware_dir}\n"
            f"source venv/bin/activate\n"
            f"python seed_om_policies.py",
            "bash", theme="monokai", line_numbers=False,
        ),
        title="[bold]Re-run OM tag-policy seeding[/bold]",
        border_style="blue",
    ))
    console.print(
        "[dim]Idempotent. Run after any tag-policy or team change in seed_om_policies.py.[/dim]"
    )


# --------------------------------------------------------------------------- #
# admin keycloak-bootstrap   (runbook)                                        #
# --------------------------------------------------------------------------- #


@app.command("keycloak-bootstrap")
def keycloak_bootstrap(
    infra_dir: str = typer.Option(
        "eunomia-infrastructure",
        "--infra-dir",
        help="Path to the eunomia-infrastructure repo.",
    ),
):
    """Print the Keycloak re-import command (in-place patching is out of scope)."""
    console.print(Panel.fit(
        Syntax(
            f"cd {infra_dir}\n"
            f"docker-compose stop keycloak\n"
            f"docker-compose rm -f keycloak\n"
            f"docker-compose up -d keycloak",
            "bash", theme="monokai", line_numbers=False,
        ),
        title="[bold]Re-import the Keycloak realm[/bold]",
        border_style="blue",
    ))
    console.print(
        "[dim]Wipes Keycloak state; re-applies keycloak/realm-export.json on startup. "
        "Active sessions are invalidated.[/dim]"
    )


# --------------------------------------------------------------------------- #
# admin verify       (runbook — wired in #26)                                 #
# --------------------------------------------------------------------------- #


@app.command("verify")
def verify(
    infra_dir: str = typer.Option(
        "eunomia-infrastructure",
        "--infra-dir",
        help="Path to the eunomia-infrastructure repo.",
    ),
):
    """Run the Phase D verification harness (lives in eunomia-infrastructure)."""
    console.print(Panel.fit(
        Syntax(
            f"cd {infra_dir}\n"
            f"python verify_phase_d.py",
            "bash", theme="monokai", line_numbers=False,
        ),
        title="[bold]Phase D verification harness[/bold]",
        border_style="blue",
    ))
    console.print(
        "[dim]Exercises every role through Keycloak → middleware → OM → MySQL, "
        "asserting the expected per-role view + PII matrix.[/dim]"
    )
