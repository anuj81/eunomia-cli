# eunomia-cli

> **Command-line client for the Eunomia governance middleware.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

A Typer-based CLI that logs in to Keycloak via OAuth 2.0 Device Code, caches tokens locally (mode 0600), refreshes them automatically, and streams natural-language queries through the Eunomia middleware over Server-Sent Events.

```
$ eunomia-cli login
   Open this URL in your browser:
     http://localhost:8080/realms/eunomia/device?user_code=ABCD-EFGH

   Enter the code: ABCD-EFGH

   (Code expires in 600s. Polling every 5s…)

   Logged in as finance.alice
     email               finance.alice@open-metadata.org
     realm_access.roles  eunomia-pii-unmask, eunomia-finance-user
     exp                 2026-05-13 02:33:00 (+3600s)

$ eunomia-cli ask "what is our daily revenue last week"
   Querying: what is our daily revenue last week
   > Authenticating & Fetching Roles...
   > Found 2 Allowed Views, using top-1 for prompt...
   > Generating SQL (Attempt 1)...
   > Validating generated SQL...
   > Executing query on MySQL...
   > Applying PII masking...

   Execution Complete!
   ╭─ Generated SQL ──────────────────────────────────────────╮
   │ SELECT order_date, total_revenue                          │
   │   FROM finance_daily_revenue_view                         │
   │  WHERE order_date >= CURDATE() - INTERVAL 5 DAY           │
   │  ORDER BY order_date ASC                                  │
   ╰───────────────────────────────────────────────────────────╯
   ┃ order_date  ┃ total_revenue ┃
   ┃ 2026-05-09  ┃ 115.09        ┃
```

---

## Commands

### User-facing

| Command | Description |
|---|---|
| `eunomia-cli login` | OIDC Device Code flow against Keycloak; tokens cached at `~/.eunomia/cli.json` |
| `eunomia-cli logout` | Clear the cached tokens |
| `eunomia-cli whoami` | Show decoded JWT claims (sub / email / roles / exp) |
| `eunomia-cli ask "<query>"` | Submit an NLQ to the middleware; auto-refresh on 401 |
| `eunomia-cli config show` | Print effective config + cache state |

### Admin (require `eunomia-om-admin` in cached JWT)

| Command | Description |
|---|---|
| `eunomia-cli admin reindex-rag [--reset]` | Trigger the RAG service `/v1/index/refresh` |
| `eunomia-cli admin whoami-om` | Probe OM `/users/loggedInUser` with the cached JWT |
| `eunomia-cli admin seed-om` | Runbook — the seeding script lives in `eunomia-middleware` |
| `eunomia-cli admin keycloak-bootstrap` | Runbook — docker-compose re-import of the Keycloak realm |
| `eunomia-cli admin verify` | Runbook — Phase D end-to-end harness in `eunomia-infrastructure` |

The action commands check the JWT's `realm_access.roles[]` before touching the network. The "runbook" commands print the exact shell command to run in the relevant repo — the CLI deliberately doesn't duplicate provisioning logic that belongs elsewhere.

---

## Quickstart

```bash
# 1. Install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Optional: override defaults at ~/.eunomia/config.toml
mkdir -p ~/.eunomia
cat > ~/.eunomia/config.toml <<TOML
[keycloak]
url = "http://localhost:8080"
realm = "eunomia"
client_id = "eunomia-cli"

[middleware]
url = "http://localhost:8000"
TOML

# 3. Log in (assumes Keycloak + middleware are up)
python -m src.main login

# 4. Ask
python -m src.main ask "show me payment history"
```

The full Eunomia stack (Keycloak, OpenMetadata, MySQL, Qdrant, the middleware, the RAG service) is brought up from [`eunomia-infrastructure`](https://github.com/anuj81/eunomia-infrastructure) + [`eunomia-middleware`](https://github.com/anuj81/eunomia-middleware) + [`eunomia-rag`](https://github.com/anuj81/eunomia-rag).

---

## Repository layout

```
eunomia-cli/
├── src/
│   ├── main.py        typer app entrypoint
│   ├── auth.py        Device Code flow + token cache (mode 0600) + refresh
│   ├── api.py         middleware SSE client w/ refresh-on-401 retry
│   ├── config.py      CliConfig + TOML override loader
│   └── admin.py       admin subcommand group with role gating
├── tests/             40 pytest cases (no live Keycloak needed)
├── pytest.ini
└── requirements.txt
```

---

## Configuration

| Lookup order | Where |
|---|---|
| 1. CLI flags | `--token`, `--rag-url`, `--rag-api-key`, etc. |
| 2. Environment | `EUNOMIA_CLI_HOME` to override the token-cache directory; `RAG_API_KEY` for `admin reindex-rag` |
| 3. `~/.eunomia/config.toml` | Per-user TOML (sections: `[keycloak]`, `[middleware]`) |
| 4. Built-in defaults | localhost dev values |

The token cache lives at `~/.eunomia/cli.json` (created with mode 0600 on save). On a near-expiry token (default <30s remaining), `auth.access_token()` triggers a refresh against Keycloak and writes back the new tokens.

---

## Development

```bash
pip install -r requirements.txt
pytest -v
```

40 tests covering: token cache roundtrip + file-mode enforcement, JWT decoder, full device-code flow happy paths and failure modes (pending → success, slow_down increases interval, access_denied, deadline expiry), refresh (happy + missing token + 4xx), `access_token` accessor (cached / auto-refresh / not-logged-in), admin role gating, admin REST happy + failure paths, runbook output assertions.

The test suite uses `pytest-httpx` to mock Keycloak and the RAG service — no live services needed.

### Token cache hygiene

`~/.eunomia/cli.json` is treated like an SSH private key:
- Written atomically (tmp file + `os.replace`) so power-loss doesn't corrupt it
- Mode set to 0600 on each write
- Cleared by `eunomia-cli logout`

If the cache is corrupt, `eunomia-cli login` overwrites it.

---

## License

[Apache 2.0](LICENSE)
