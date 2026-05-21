# Changelog

Version history of `appflowy-mcp`. Format is informal; we record what changed and why.

## 0.9.0 — 2026-05-21

### Added
- `search_pages(workspace_id, query, ...)` tool. Walks every Document-layout page in the workspace, scans plain text (formatting stripped), returns short snippets per matching page sorted by match count. Pages with no match are omitted; full bodies stay on the server until the client explicitly calls `read_page`.
- Supports substring (default) and Python regex (`use_regex=True`), case-sensitive flag, and a tunable snippet width (`snippet_chars`).
- New `extract_plain_text(collab_json)` helper in [markdown.py](src/appflowy_mcp/markdown.py): document text without markdown marks (so a query like `*foo*` does not false-positive on italics rendering).

### Design notes
- Cost model: server walks all pages and decodes each Y.Doc with pycrdt; the agent only pays tokens for the returned snippets. Concurrency capped at 8 in-flight fetches per call to avoid hammering AppFlowy. No cache yet — every call re-walks. Acceptable at team scale (a few hundred pages); revisit when latency starts to bite.
- Why a custom search and not `GET /api/search/{workspace_id}`: the native endpoint is semantic (OpenAI embeddings) and requires the `appflowy_ai` service + an API key, which we deliberately keep stopped. Substring search covers the "find me everything about X" workflow without any token spend on the server side.

### Reminder
- New tool — every Claude Code (or other MCP) client must restart its session after pulling the new image. The tool list is requested at connect time.

## 0.8.0 — 2026-05-20 — **breaking**

### Changed
- Per-user auth instead of a single shared bot. The server now reads `X-AppFlowy-Email` and `X-AppFlowy-Password` from each MCP HTTP request and runs every tool under the caller's AppFlowy identity. An internal pool caches one `AppFlowyClient` per `(email, password)` so token state is isolated per user.
- `Config` no longer reads `APPFLOWY_BOT_EMAIL` / `APPFLOWY_BOT_PASSWORD`. Removed from `docker-compose.override.yml`.
- stdio transport refused at startup — per-user headers require streamable-HTTP.

### Migration
- Drop `APPFLOWY_MCP_BOT_EMAIL`/`APPFLOWY_MCP_BOT_PASSWORD` from `appflowy-cloud/.env`.
- Each MCP client must send `X-AppFlowy-Email` and `X-AppFlowy-Password` headers. Example: `claude mcp add --transport http --header "X-AppFlowy-Email: alice@..." --header "X-AppFlowy-Password: ..." appflowy http://host:8765/mcp`.
- TLS becomes mandatory once exposed beyond localhost (credentials travel in headers on every request) — terminate at a reverse proxy.

## 0.7.2 — 2026-05-20

### Changed
- Rolled `replace_page_content` back to `PUT /api/workspace/{ws}/collab/{obj}` (DB upsert) after 0.7.0–0.7.1 failed.
- The `replace_page_content` docstring now explicitly warns: close all AppFlowy tabs for the page **before** writing, otherwise the active WebSocket session will overwrite the upsert.

## 0.7.1 — 2026-05-20 — **broken**

### Changed
- Attempt #2: send the full state of the Y.Doc as an update (`doc.get_update()` without a state_vector) via `POST /v1/.../web-update`. The idea was that Yrs Y.Map last-writer-wins would resolve the conflict with the old `document`.
- Did not work: request 200, but neither the UI nor the DB picked it up (same as 0.7.0).

## 0.7.0 — 2026-05-20 — **broken**

### Added
- Experiment: `replace_page_content` via the realtime channel `POST /api/workspace/v1/{ws}/collab/{obj}/web-update`. Goal: make writes work live, without having to close AppFlowy tabs.
- New client method `apply_doc_update_web()` + helper `build_replacement_update()` in `doc_builder.py`. Logic: load existing → snapshot state_vector → `del data["document"]` (CRDT tombstone) → put new → `doc.get_update(sv_before)` → POST.
- Server `publish_update` (Redis stream → broadcast to WS clients) **accepts the request** (200 OK), but with no effect — hypothesis around the update format vs. the expected Yjs sync-protocol, or a client_id mismatch.

## 0.6.3 — 2026-05-20

### Fixed
- The root `page` block must not have `external_id`/`external_type` either (applied the rule from 0.6.2 to it as well). Before this, the root was written with `external_id: ""` — the UI treated it as "has text", and the page opened empty.

## 0.6.2 — 2026-05-20

### Fixed
- The `simple_table` block must have `rowsLen` and `colsLen` in `data` — otherwise the AppFlowy UI does not draw the table. (The constants are defined in `collab-document/src/importer/define.rs`, but the importer itself does not set them. The UI still expects them.)
- `simple_table` / `simple_table_row` / `simple_table_cell` must have **missing** `external_id`/`external_type`, not empty strings. Before this the UI tried to render the block as "has text", found nothing, and broke.
- In pycrdt, `Map(...)` fields are set explicitly — omitting a key is not the same as an empty string.

## 0.6.1 — 2026-05-20

### Fixed
- **UTF-8 byte offsets in pycrdt.** `Text.format(start, end, attrs)` uses BYTE indices, not char indices. For Cyrillic / emoji this produced shifted ranges and formatting that "slid" into the middle of words. Compute `len(chunk.encode("utf-8"))`.
- Escaped `\|` in a markdown table is now correctly NOT cut as a cell separator. Before this `\| col \|` broke parsing.

## 0.6.0 — 2026-05-20

### Added
- Markdown tables. The parser assembles the nesting `simple_table → simple_table_row → simple_table_cell → paragraph`, with per-column alignment (`:---:` / `---:` / `:---`).
- Reverse render of `simple_table` in `read_page` — outputs pipes-and-dashes markdown.
- Inline formatting is preserved inside cells (the text lives in a paragraph inside the cell, parse_inline is applied as usual).
- The schema and the fields (`rowPosition`, `colPosition`, `align`) are taken from `collab-document/src/importer/md_importer.rs` and `define.rs`.

## 0.5.0 — 2026-05-20

### Added
- **Inline formatting** for markdown: `**bold**`, `*italic*`, `` `code` ``, `[text](url)`, `~~strike~~`.
- New module [inline.py](src/appflowy_mcp/inline.py): a regex parser produces `[(chunk, attrs), ...]` runs.
- `doc_builder` now applies formatting through **`Text.format(start, end, attrs)` ranges**, not `insert(chunk, attrs=...)` — the latter merges adjacent inserts under a shared attribute (Yrs semantics).
- `read_page` for the Document layout now does **not go through `/collab/json`** — it pulls the raw `encoded_collab` from `/page-view` and decodes it with pycrdt. Reason: the server-side `to_json_value()` flattens Y.Text into a plain string, losing deltas. The pycrdt diff returns separate runs with attrs.

## 0.4.0 — 2026-05-20

### Added
- **Page writes** via `replace_page_content`. Markdown → block tree → pycrdt Y.Doc following the AppFlowy-Collab schema → bincode `EncodedCollab` wrapper → `PUT /api/workspace/{ws}/collab/{obj}` with `encoded_collab_v1`.
- New module [doc_builder.py](src/appflowy_mcp/doc_builder.py): Y.Doc assembly + manual `EncodedCollab` serialization through struct (8-byte LE lengths + bytes + 1-byte version tag).
- New module [markdown_to_blocks.py](src/appflowy_mcp/markdown_to_blocks.py): a line-by-line parser. Supports headings, paragraphs, bulleted/numbered/todo lists with nesting (2-space indent), quotes, fenced code, dividers.
- pycrdt dependency.

### Discovered
- AppFlowy-Collab is cloned separately into [../appflowy-collab/](../appflowy-collab/) at revision `e59260e` (the same as the `Cargo.toml` in `appflowy-cloud`).
- The Y.Doc root key is `data`, not `document` (you have to `doc["data"] = Map({})` BEFORE `apply_update`, otherwise the data lands in the default container and `doc["document"]` is empty).
- `EncodedCollab { state_vector, doc_state, version }` is a bincode-serialized struct for `PUT`. `state_vector` is mandatory, otherwise the server does not parse.
- `encoded_collab` from `/page-view` is already a **raw doc_state**, not wrapped in bincode (an asymmetry with `PUT`).

## 0.3.0 — 2026-05-20

### Added
- `rename_page(workspace_id, view_id, new_name)` — `POST /api/workspace/{ws}/page-view/{view}/update-name`.

### Decided
- Do not add `delete_page` / `move_page` by default. The user explicitly asked to defer: "I would not give destructive operations to an AI yet." Read + rename — yes; delete/move — not now.

## 0.2.0 — 2026-05-20

### Added
- `create_page(workspace_id, parent_view_id, name, layout)` — `POST /api/workspace/{ws}/page-view` with `CreatePageParams`. Layout as a string ("Document"/"Grid"/"Board"/"Calendar"/"Chat"), mapped to int.

## 0.1.0 — 2026-05-19

### Added
- First working version. 3 read tools:
  - `list_workspaces()` — `GET /api/workspace`
  - `list_pages(workspace_id, depth)` — `GET /api/workspace/{ws}/folder?depth=N`
  - `read_page(workspace_id, view_id)` — `GET /page-view/{view}` + `GET /collab/{view}/json?collab_type=N` → markdown
- AppFlowy HTTP client with auth (GoTrue password grant + verify bootstrap) and token refresh.
- FastMCP server with two transports: stdio (dev) and streamable-http (the container on `:8765/mcp`).
- Docker packaging + integration into the AppFlowy-Cloud docker-compose stack (override.yml).
- Markdown render from `collab/json` (supports headings, paragraphs, lists, todos, code, quotes, dividers, child_page, toggle, callout, image, plus Yjs delta-string parsing for inline formatting at read time).
