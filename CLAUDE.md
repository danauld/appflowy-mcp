# appflowy-mcp — MCP server for AppFlowy

An MCP server that gives LLM agents (Claude Code, Cline, Claude Desktop, ...) tools for reading and writing documents in a self-hosted AppFlowy instance.

> This file is the living context of the MCP server itself. The deployment context (TLS, GoTrue, the docker-compose stack) lives in [../CLAUDE.md](../CLAUDE.md).

## What is inside

A thin wrapper around the AppFlowy-Cloud REST API plus native Yrs CRDT document assembly on pycrdt. Per-user auth model: every MCP HTTP request carries `X-AppFlowy-Email` / `X-AppFlowy-Password` headers, and tools run under the caller's AppFlowy identity. No shared bot. 7 MCP tools:

| Tool | What it does | AppFlowy endpoint |
|---|---|---|
| `list_workspaces` | list of the user's workspaces | `GET /api/workspace` |
| `list_pages` | view tree | `GET /api/workspace/{ws}/folder` |
| `read_page` | page → markdown | `GET /api/workspace/{ws}/page-view/{view}` + decode raw `encoded_collab` with pycrdt (to preserve deltas) |
| `search_pages` | substring/regex search across all Document pages → snippets | folder walk + per-page `get_document_decoded` + plain-text extraction; server-side scan, only snippets returned |
| `create_page` | new empty page | `POST /api/workspace/{ws}/page-view` |
| `rename_page` | rename | `POST /api/workspace/{ws}/page-view/{view}/update-name` |
| `replace_page_content` | markdown → write | `PUT /api/workspace/{ws}/collab/{obj}` with `encoded_collab_v1` (a bincode wrapper around the Y.Doc) |

## Folder layout

```
appflowy-mcp/
├── CLAUDE.md                     # this file
├── CHANGELOG.md                  # version history
├── pyproject.toml                # python package (hatchling)
├── Dockerfile                    # python:3.12-slim + pip install -e .
├── docker-compose.example.yml    # fragment to drop into the AppFlowy-Cloud stack
├── .env                          # creds for local dev (gitignored)
├── .env.example                  # template
├── smoke_test.py                 # manual client check bypassing MCP (gitignored output rendered.md/collab_dump.json/etc)
└── src/appflowy_mcp/
    ├── __init__.py
    ├── __main__.py               # entry point: loads .env, runs FastMCP
    ├── config.py                 # env vars → Config dataclass
    ├── client.py                 # async httpx + auth (token cache/refresh + verify bootstrap)
    ├── server.py                 # FastMCP server + tool definitions
    ├── markdown.py               # AppFlowy JSON AST → markdown (for read_page)
    ├── markdown_to_blocks.py     # markdown → AppFlowy block tree (for write)
    ├── doc_builder.py            # block tree → pycrdt Y.Doc → bincode bytes
    └── inline.py                 # inline markdown (bold/italic/code/link/strike) → runs[(text, attrs)]
```

## Build and deploy

The image is built with the tag `appflowy-mcp:X.Y.Z` (the same as the `version` in `pyproject.toml`).

```powershell
# From the stack root (where docker-compose lives):
cd x:\Projects\AppFlowy\appflowy-cloud
docker compose build appflowy_mcp
docker compose up -d appflowy_mcp

# Local dev in a venv:
cd x:\Projects\AppFlowy\appflowy-mcp
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\python.exe smoke_test.py
```

On **every** code change:
1. Bump `version` in `pyproject.toml`.
2. Bump the tag `image: appflowy-mcp:X.Y.Z` in [../appflowy-cloud/docker-compose.override.yml](../appflowy-cloud/docker-compose.override.yml).
3. `docker compose build appflowy_mcp && docker compose up -d appflowy_mcp`.
4. Add an entry to [CHANGELOG.md](CHANGELOG.md).
5. If a **new tool** is added — the client (Claude Code etc.) **must restart its session**: the tool list is requested at connect time, new tools are not picked up without a restart.

## Configuration (env vars)

See [.env.example](.env.example):
- `APPFLOWY_BASE_URL` — for the container inside the stack this is `http://nginx` (internal gateway); for local dev — `https://localhost`.
- `APPFLOWY_TLS_VERIFY` — `false` for self-signed on localhost.
- `APPFLOWY_MCP_TRANSPORT` — must be `http` (per-user auth requires the HTTP transport to read headers).
- `APPFLOWY_MCP_HOST` / `APPFLOWY_MCP_PORT` — bind address (default `0.0.0.0:8765`).

The server has **no static credentials**. Per-user identity comes from the `X-AppFlowy-Email` and `X-AppFlowy-Password` headers on each MCP request. `smoke_test.py` reads `APPFLOWY_BOT_EMAIL`/`APPFLOWY_BOT_PASSWORD` directly from the environment to call the client outside the MCP layer — those env vars are dev-only and not consumed by the server itself.

## Per-user auth

- Implemented as a `ClientPool` in [server.py](src/appflowy_mcp/server.py).
- Reads headers from `ctx.request_context.request.headers` (FastMCP exposes the underlying Starlette `Request` to tool handlers).
- Cache key is `(email, password)`. A password change creates a fresh entry; the old one stays in memory until process restart (acceptable for the team-scale we target).
- Each cached `AppFlowyClient` has its own `_auth_lock`, so parallel calls from the same user serialise only across refresh, not normal requests.
- TLS must be terminated in front of the server (credentials are in headers on every request). The stack's nginx terminates TLS via a `location /mcp` block that proxies to `appflowy_mcp:8765` with `proxy_buffering off` (MCP streams responses as SSE). Recommended URL: `https://your-host/mcp`. The raw HTTP port `8765` stays published for dev only.

## AppFlowy document Y.Doc schema

The source of truth is `appflowy-collab/collab-document/src/` (cloned into [../appflowy-collab/](../appflowy-collab/) at revision `e59260e`).

```
Y.Doc.data (Map):                     ← the root key is `data`, NOT `document`!
  "document" (Map):
    "page_id" → String
    "blocks" (Map):
      <block_id> (Map):
        "id" → String
        "ty" → String                 (paragraph, heading, todo_list, simple_table, ...)
        "parent" → String             (parent block_id)
        "children" → String           (key into children_map)
        "data" → String               (JSON-stringified attrs: {"level":1}, {"checked":true}, ...)
        "external_id"   → String      (key into text_map; ABSENT for blocks without text)
        "external_type" → String      ("text"; ABSENT for blocks without text)
    "meta" (Map):
      "children_map" (Map):
        <key> → Y.Array<String>       (ordered block_ids)
      "text_map" (Map):
        <key> → Y.Text                (with deltas: bold/italic/strikethrough/code/href/mention)
```

### Critical gotchas (hard-won)

- **The root key is `data`, not `document`.** In Rust: `collab.data.get_or_init_map(.., DOCUMENT_ROOT)` — meaning `document` is a key INSIDE `data`.
- **pycrdt `Text.format(start, end)` uses UTF-8 BYTE offsets, not char indices.** For Cyrillic / emoji (1 char ≠ 1 byte) char indices give shifted ranges and broken formatting. Compute `len(chunk.encode("utf-8"))`. See `doc_builder.py`.
- **Inline formatting: NOT `insert(chunk, attrs=...)` calls in a row.** pycrdt merges adjacent inserts under a shared attribute. The right way: insert all the plain text first, then `text.format(start, end, attrs)` over ranges.
- **Blocks without text (`page`, `divider`, `simple_table*`) must not have `external_id`/`external_type` in the Y.Map.** Not as empty strings, but as **missing keys**. The UI treats `external_id: ""` as "has text" and tries to render it, then falls into an empty page.
- **Tables**: `simple_table.data` must have `rowsLen` and `colsLen` — otherwise the UI does not render. Structure: `simple_table → simple_table_row → simple_table_cell → paragraph`. Text lives ONLY in the paragraph inside the cell — that is also where inline formatting works.
- **`encoded_collab` from `/page-view`** is already a **raw Yrs v1 update** (`doc_state` extracted from EncodedCollab). Not bincode-wrapped. Apply directly via `pycrdt.Doc.apply_update(bytes)`.
- **`encoded_collab_v1` in `PUT /collab/{obj}`** is a **bincode-serialized `EncodedCollab { state_vector, doc_state, version }`**. The wrapper is mandatory. Format: `[u64 sv_len LE][sv_bytes][u64 ds_len LE][ds_bytes][u8 version]`. See `_encode_encoded_collab` in [doc_builder.py](src/appflowy_mcp/doc_builder.py).
- **Auth bootstrap**: after `POST /gotrue/token` it is mandatory to call `GET /api/user/verify/{access_token}` — otherwise the user exists in GoTrue but NOT in `af_user`, and `list_workspaces` returns empty.
- **The JSON output of `/collab/json` flattens Y.Text into a plain string** (deltas are lost). For `read_page` we pull the raw `encoded_collab` from `/page-view` and decode it with pycrdt — the diff with attrs is preserved there.

## Main limitation: writes conflict with active WS sessions

`replace_page_content` uses `PUT /api/workspace/{ws}/collab/{obj}` — this is a **background upsert into postgres**. If the page is open in a browser/desktop client, the active WebSocket session has its own local Y.Doc, and on its next sync through the realtime server it **overwrites our upsert** (its local state "wins").

**Workaround**: before `replace_page_content`, close all AppFlowy tabs for this page; open them again afterwards.

The attempt to switch to `POST /v1/.../web-update` (the realtime channel AppFlowy Web uses for its own edits) happened in 0.7.0–0.7.1 — the request returns 200, but neither the UI nor the DB picks up our updates. Hypotheses: (a) the Yrs update format from pycrdt differs from what `publish_update` expects, (b) a client_id problem (we are a "one-time web user", not an active session), (c) the message must be a Yjs sync-protocol message, not a bare update. Reverted to PUT in 0.7.2; a deep investigation is a separate TODO.

## How to add a new tool

1. Method in [client.py](src/appflowy_mcp/client.py) — calls the relevant AppFlowy endpoint.
2. `@mcp.tool()` in [server.py](src/appflowy_mcp/server.py) — the docstring is **critical** (the LLM uses it to decide when to call the tool and what arguments to pass). **Take `ctx: Context` as the first parameter** and call `client = await pool.get(ctx)` at the top of the body to obtain the per-user `AppFlowyClient`.
3. Bump `version` in [pyproject.toml](pyproject.toml) and the tag in [../appflowy-cloud/docker-compose.override.yml](../appflowy-cloud/docker-compose.override.yml).
4. `docker compose build appflowy_mcp && docker compose up -d appflowy_mcp`.
5. Append to [CHANGELOG.md](CHANGELOG.md).
6. The MCP client user (Claude Code etc.) **restarts the session** — otherwise the new tool is invisible.

## Principles

- **Do not give the LLM destructive operations by default** (delete/move/wipe are deferred). Read and rename are fine; content edits come with a warning about replacement.
- **Tool names and docstrings matter more than the implementation** — they are the interface to the LLM. Change them carefully.
- **The schema is reverse-engineered, not official** — AppFlowy does not publish an MCP spec; everything was figured out by reading Rust sources. When upgrading AppFlowy-Cloud, re-check the `appflowy-collab/` rev and verify that the schema has not shifted.
- **All documentation (this file, CHANGELOG.md, README.md) must be written in English.**
