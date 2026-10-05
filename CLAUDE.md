# appflowy-mcp — MCP server for AppFlowy

An MCP server that gives LLM agents (Claude Code, Cline, Claude Desktop, ...) tools for reading and writing documents in a self-hosted AppFlowy instance.

> This file is the living context of the MCP server itself. The deployment context (TLS, GoTrue, the docker-compose stack) lives in [../CLAUDE.md](../CLAUDE.md).

## What is inside

A thin wrapper around the AppFlowy-Cloud REST API plus native Yrs CRDT document assembly on pycrdt. Per-user auth model: every MCP HTTP request carries `X-AppFlowy-Email` / `X-AppFlowy-Password` headers, and tools run under the caller's AppFlowy identity. No shared bot. 20 MCP tools:

| Tool | What it does | AppFlowy endpoint |
|---|---|---|
| `list_workspaces` | list of the user's workspaces | `GET /api/workspace` |
| `list_pages` | view tree | `GET /api/workspace/{ws}/folder` |
| `read_page` | page / card body → markdown | `GET /api/workspace/{ws}/page-view/{view}` (or row body via `uuid5(row_id, "document_id")`) + decode raw `encoded_collab` with pycrdt |
| `search_pages` | substring/regex search across all Document pages → snippets | folder walk + per-page `get_document_decoded` + plain-text extraction; server-side scan, only snippets returned |
| `create_page` | new empty page | `POST /api/workspace/{ws}/page-view` |
| `rename_page` | rename | `POST /api/workspace/{ws}/page-view/{view}/update-name` |
| `reorder_page` | reorder a page within its current parent (top/bottom/after:/before:) | folder walk to find current parent → resolve `prev_view_id` → `POST /api/workspace/{ws}/page-view/{view}/move` |
| `move_page` | move a page under a different parent (cross-section move) with optional position | folder walk to resolve `prev_view_id` against new parent's children → `POST /api/workspace/{ws}/page-view/{view}/move` |
| `replace_page_content` | full rewrite from markdown (accepts view_id or row_id) | load existing → replace root children in place → incremental update via `POST .../web-update`; `PUT /collab/{obj}` with a fresh `encoded_collab_v1` only when the document does not exist yet |
| `append_to_page` | append markdown to the end of a page or card body (existing content preserved) | load existing `encoded_collab` → mutate root children Y.Array via pycrdt → incremental update via `POST .../web-update` |
| `replace_section` | replace one root-level section by heading-text match | load existing → find heading → delete range → insert parsed new blocks → incremental update via `web-update` |
| `insert_after_heading` | insert new blocks immediately after a root-level heading | load existing → find heading → insert parsed new blocks at index+1 → incremental update via `web-update` |
| `insert_before_heading` | insert new blocks immediately before a root-level heading | load existing → find heading → insert parsed new blocks at index → incremental update via `web-update` |
| `list_databases` | list databases (Grid/Board/Calendar) | `GET /api/workspace/{ws}/database` |
| `get_database_fields` | read fields, types, options, and relation target DBs | `GET /api/workspace/{ws}/database/{db}/fields` |
| `get_database_rows` | read rows with pagination, search, doc bodies, and relation titles | `GET .../row` + `GET .../row/detail` + `collab_type: 4` for relation resolution |
| `create_database_row` | append a new row; cells validated first, Relation cells written via the row collab afterwards | `POST /api/workspace/{ws}/database/{db}/row` (+ `web-update`, `collab_type: 4`, for relations) |
| `upsert_database_row` | insert-or-update keyed by pre_hash (reaches only rows first written with that key); cells validated first, Relation cells via the row collab | `PUT /api/workspace/{ws}/database/{db}/row` (+ `web-update` for relations) |
| `update_database_row` | in-place update of an existing row by row ID | `POST .../collab/{row_id}/web-update` (`collab_type: 4`) |
| `add_select_option` | add option to SingleSelect/MultiSelect column | `POST .../collab/{database_id}/web-update` (`collab_type: 1`) |

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
├── smoke_test.py                 # manual client check bypassing MCP
├── tests/                        # unit tests
│   └── test_v015.py
└── src/appflowy_mcp/
    ├── __init__.py
    ├── __main__.py               # entry point: loads .env, runs FastMCP
    ├── config.py                 # env vars → Config dataclass
    ├── client.py                 # async httpx + auth (token cache/refresh + verify bootstrap)
    ├── server.py                 # FastMCP server + tool definitions
    ├── database_collab.py        # database & row CRDT manipulation (relations, select options, in-place edit)
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
- **Relation cells are an array, not a string.** collab-database's `RelationCellData::from(&Cell)` reads `data` only when it is `Any::Array` of UUID strings and returns an empty relation otherwise. Write a Python list through pycrdt (stored as `Any::Array`); never `json.dumps(...)`. Versions <= 0.15.1 wrote a JSON string that the UI showed as empty; `scripts/repair_relation_cells.py` fixes old cells.
- **The server is stateless streamable-HTTP** (`stateless_http=True`): no `Mcp-Session-Id` is required or honoured, which is what the cloud connectors' session-less `tools/list` probes need.
- **Auth bootstrap**: after `POST /gotrue/token` it is mandatory to call `GET /api/user/verify/{access_token}` — otherwise the user exists in GoTrue but NOT in `af_user`, and `list_workspaces` returns empty.
- **The JSON output of `/collab/json` flattens Y.Text into a plain string** (deltas are lost). For `read_page` we pull the raw `encoded_collab` from `/page-view` and decode it with pycrdt — the diff with attrs is preserved there.

## Writes: realtime channel first, PUT only as fallback

Since 0.17.0 every edit of an existing document or row goes through `POST /api/workspace/v1/{ws}/collab/{obj}/web-update` with an **incremental Yrs update computed from the server's current state** (`doc.get_state()` before the mutation, `doc.get_update(sv)` after). The realtime server applies it, persists it, and broadcasts it, so an open AppFlowy window shows the change at once and cannot overwrite it, and only the delta travels. Verified live on 2026-10-05.

`PUT /api/workspace/{ws}/collab/{obj}` (full `encoded_collab_v1`, a background upsert) remains for two cases only: creating a document that does not exist yet (`build_document`), and the fallback when web-update refuses (`_write_document` in server.py logs a warning and reports `write_path: "put"`). The PUT route is capped at 5 MB by AppFlowy-Cloud and the body is a JSON integer array, so it fails for documents above roughly 1.1 MB of state, and a live editor's next sync can overwrite it.

Why the 0.7.0 attempt failed: `build_replacement_update` deleted the `document` key and re-created it, then sent the full state. Open editors held the old `document` map and never adopted the new one. Mutating the existing maps and sending the delta is what works; never re-create `document`.

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
