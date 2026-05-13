"""eunomia-cli — user-facing CLI for the Eunomia Middleware.

Commands:
    eunomia-cli login            OIDC Device Code flow against Keycloak; cache tokens
    eunomia-cli logout           clear cached tokens
    eunomia-cli whoami           decode + display cached identity
    eunomia-cli ask <query>      ask the middleware an NLQ
    eunomia-cli config show      print the effective config
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import admin as admin_module
from . import api, auth
from .config import CONFIG_FILE, TOKEN_FILE, load_config

app = typer.Typer(help="Eunomia CLI — Phase D")
console = Console()
app.add_typer(admin_module.app, name="admin")


# --------------------------------------------------------------------------- #
# login / logout / whoami                                                     #
# --------------------------------------------------------------------------- #


@app.command()
def login():
    """Acquire a Keycloak access token via OIDC Device Code flow."""
    cfg = load_config()
    console.print(f"[dim]Keycloak: {cfg.issuer}[/dim]")

    try:
        start = auth.begin_device_flow(cfg)
    except auth.AuthError as e:
        console.print(f"[bold red]Could not start device flow:[/bold red] {e}")
        raise typer.Exit(1)

    # Show user-facing instructions.
    verification = start.verification_uri_complete or start.verification_uri
    console.print()
    console.print(
        Panel.fit(
            f"[bold]1.[/bold] Open this URL in your browser:\n"
            f"   [link={verification}]{verification}[/link]\n\n"
            f"[bold]2.[/bold] Enter the code: [bold yellow]{start.user_code}[/bold yellow]\n\n"
            f"[dim](Code expires in {start.expires_in}s.  Polling every {start.interval}s…)[/dim]",
            title="[bold blue]Approve in your browser[/bold blue]",
            border_style="blue",
        )
    )

    try:
        bundle = auth.poll_device_token(cfg, start)
    except auth.DeviceFlowAborted as e:
        console.print(f"[bold red]Login aborted:[/bold red] {e}")
        raise typer.Exit(1)
    except auth.AuthError as e:
        console.print(f"[bold red]Login failed:[/bold red] {e}")
        raise typer.Exit(1)

    auth.save_tokens(bundle)
    claims = auth.decode_claims(bundle.access_token)
    who = claims.get("preferred_username") or claims.get("sub", "(unknown)")
    console.print(f"\n[bold green]Logged in as[/bold green] {who}")
    _print_claims(claims)


@app.command()
def logout():
    """Remove the cached token bundle."""
    removed = auth.clear_tokens()
    if removed:
        console.print("[bold green]Logged out.[/bold green]  Cache file removed.")
    else:
        console.print("[dim]Already logged out (no cache file).[/dim]")


@app.command()
def whoami():
    """Show the cached identity (decoded JWT claims, not re-verified)."""
    claims = auth.cached_identity()
    if claims is None:
        console.print("[dim]Not logged in.[/dim]  Run `eunomia-cli login`.")
        raise typer.Exit(1)
    who = claims.get("preferred_username") or claims.get("sub", "(unknown)")
    console.print(f"[bold green]{who}[/bold green]")
    _print_claims(claims)


def _print_claims(claims: dict) -> None:
    """Pretty-print the key identity + expiry claims."""
    exp = claims.get("exp")
    exp_str = "(no exp)"
    if exp:
        delta = int(exp) - int(time.time())
        when = dt.datetime.fromtimestamp(int(exp))
        if delta > 0:
            exp_str = f"{when:%Y-%m-%d %H:%M:%S} ([green]+{delta}s[/green])"
        else:
            exp_str = f"{when:%Y-%m-%d %H:%M:%S} ([red]expired {-delta}s ago[/red])"

    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column(style="cyan")
    t.add_column()
    t.add_row("email",              str(claims.get("email") or "-"))
    t.add_row("preferred_username", str(claims.get("preferred_username") or "-"))
    t.add_row("realm_access.roles", ", ".join((claims.get("realm_access") or {}).get("roles") or []))
    t.add_row("iss",                str(claims.get("iss") or "-"))
    t.add_row("exp",                exp_str)
    console.print(t)


# --------------------------------------------------------------------------- #
# config                                                                      #
# --------------------------------------------------------------------------- #


config_app = typer.Typer(help="Inspect / manage CLI config.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show():
    """Print the effective config."""
    cfg = load_config()
    t = Table(title="eunomia-cli config", show_header=False, box=None, padding=(0, 1))
    t.add_column(style="cyan"); t.add_column()
    t.add_row("config_file",   str(CONFIG_FILE) + ("  [dim](missing — using defaults)[/dim]" if not CONFIG_FILE.exists() else ""))
    t.add_row("token_cache",   str(TOKEN_FILE) + ("  [dim](no active session)[/dim]" if not TOKEN_FILE.exists() else "  [green](active)[/green]"))
    t.add_row("keycloak_url",  cfg.keycloak_url)
    t.add_row("realm",         cfg.realm)
    t.add_row("client_id",     cfg.client_id)
    t.add_row("middleware_url", cfg.middleware_url)
    console.print(t)


# --------------------------------------------------------------------------- #
# ask                                                                         #
# --------------------------------------------------------------------------- #


@app.command()
def ask(
    query: str,
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="Bypass cache and use this bearer token explicitly (debug only).",
    ),
):
    """Ask a natural-language question through the Eunomia middleware."""
    cfg = load_config()
    console.print(f"[bold blue]Querying:[/bold blue] {query}")

    try:
        events = api.stream_nlq(cfg, query, override_token=token)
        for ev in events:
            _render_event(ev)
    except auth.NotLoggedInError as e:
        console.print(f"[bold red]Not logged in:[/bold red] {e}")
        raise typer.Exit(2)
    except httpx.ConnectError:
        console.print(
            "[bold red]Could not connect to middleware.[/bold red]  "
            f"Is it running on {cfg.middleware_url}?"
        )
        raise typer.Exit(3)
    except httpx.HTTPStatusError as e:
        console.print(f"[bold red]Middleware returned HTTP {e.response.status_code}[/bold red]: {e.response.text[:200]}")
        raise typer.Exit(4)


def _render_event(ev: api.StreamEvent) -> None:
    """Render a single SSE event with rich-flavored output."""
    if ev.event == "complete":
        data = ev.data
        if data.get("status") == "error":
            # Error envelope that terminates the flow
            console.print(f"\n[bold red]✗[/bold red] {data.get('message') or '(no message)'}")
            req_id = data.get("request_id")
            if req_id:
                console.print(f"[dim]request_id={req_id}[/dim]")
            return

        console.print("\n[bold green]Execution Complete![/bold green]")
        sql = data.get("executed_sql") or "(no SQL returned)"
        console.print(Panel(
            Syntax(sql, "sql", theme="monokai", line_numbers=False),
            title="[bold blue]Generated SQL[/bold blue]",
            border_style="blue",
        ))

        results = data.get("results") or []
        if results:
            t = Table(show_header=True, header_style="bold magenta")
            for k in results[0].keys():
                t.add_column(k)
            for row in results:
                t.add_row(*[str(v) for v in row.values()])
            console.print(t)
        else:
            console.print("[dim]No rows returned.[/dim]")

        req_id = data.get("request_id")
        if req_id:
            console.print(f"\n[dim]request_id={req_id}[/dim]")
        return

    # Status / progress event
    status = ev.data.get("status") or ev.data.get("raw") or ""
    if status:
        # Error mid-stream (some routes yield {"status": "error", ...} without "complete" event)
        if status == "error":
            console.print(f"[bold red]✗[/bold red] {ev.data.get('message') or '(no message)'}")
        else:
            console.print(f"[cyan]>[/cyan] {status}")


# --------------------------------------------------------------------------- #
# entrypoint                                                                  #
# --------------------------------------------------------------------------- #


def main() -> None:
    app()


if __name__ == "__main__":
    main()
