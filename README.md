# appflowy-mcp

An MCP server that lets LLM agents (Claude Code, Cline, Claude Desktop, ...) read
and write pages in a self-hosted [AppFlowy-Cloud](https://github.com/AppFlowy-IO/AppFlowy-Cloud)
instance.

The server runs centrally next to AppFlowy-Cloud, but every MCP call is
authenticated **per user**: each request carries the caller's own AppFlowy
email and password as HTTP headers, and the tool executes under that user's
identity and permissions. No shared bot account, no extra sharing chore for
admins.

## Tools

| Tool | Purpose |
|---|---|
| `list_workspaces()` | All workspaces the calling user can see |
| `list_pages(workspace_id, depth=10)` | Folder tree of a workspace |
| `read_page(workspace_id, view_id)` | Document content as Markdown (or Grid/Board schema as JSON) |
| `create_page(workspace_id, parent_view_id, name, layout="Document")` | Create an empty page |
| `rename_page(workspace_id, view_id, new_name)` | Rename a page |
| `replace_page_content(workspace_id, view_id, markdown_content)` | Replace a Document page's body with Markdown |

`replace_page_content` accepts headings, paragraphs, bulleted/numbered/todo lists
with indent-based nesting (2 spaces/level), block quotes, fenced code blocks,
horizontal rules, simple tables with column alignment, and inline
**bold**/*italic*/`code`/~~strike~~/[links](url).

There are no delete/move tools — by design. Reads + additive edits only.

---

## For admins — deploying the server

The server is packaged as a Docker image and is designed to run next to
AppFlowy-Cloud (same Compose stack), exposing a streamable-HTTP MCP endpoint
that team members connect their agents to. There is no bot account to create
and no permissions to share — users connect with their own AppFlowy logins.

### Add the service to your Compose stack

Copy [docker-compose.example.yml](docker-compose.example.yml) into your
AppFlowy-Cloud `docker-compose.yml` (or include it via an override file). No
bot credentials are required — the service is stateless with respect to user
identity.

```bash
docker compose up -d appflowy_mcp
docker compose logs -f appflowy_mcp
```

`APPFLOWY_BASE_URL` inside the container points at the internal `appflowy_cloud`
service, bypassing nginx/TLS. The MCP endpoint itself is plain HTTP on port
`8765` — **terminate TLS at your reverse proxy** if exposing beyond localhost,
because each MCP request carries a user's email and password in headers.

### Hand the endpoint URL to your team

Whatever `https://host:8765/mcp` (or whatever your TLS-terminated URL is)
resolves to from the team's machines — that's what every user puts in their
agent config, along with their own AppFlowy credentials.

---

## For users — connecting your agent

You need:
- **The URL** of the MCP server (from your admin).
- Your own **AppFlowy email and password** — whatever you log into AppFlowy
  with.
- An agent that speaks MCP over HTTP and can send custom HTTP headers.

### Claude Code

```bash
claude mcp add --transport http \
  --header "X-AppFlowy-Email: you@your.domain" \
  --header "X-AppFlowy-Password: your-appflowy-password" \
  appflowy https://your-server:8765/mcp
claude mcp list   # appflowy ✓ Connected
```

Restart the Claude Code session so it picks up the tools.

### Claude Desktop / Cline / others

Add an HTTP MCP server pointing at the same URL, and configure it to send the
two custom headers `X-AppFlowy-Email` and `X-AppFlowy-Password` on every
request. Consult your client's docs for the exact JSON shape — the transport
is "streamable HTTP" (the MCP spec default for HTTP).

### Verifying

Ask your agent to call `list_workspaces` — you should see every workspace you
can see in AppFlowy itself. If you get a "Missing X-AppFlowy-Email..." error,
your client is not sending the headers; double-check your MCP config.

---

## Configuration

All settings come from environment variables (`.env` is loaded in dev mode for
convenience). See [.env.example](.env.example).

| Var | Required | Default | Notes |
|---|---|---|---|
| `APPFLOWY_BASE_URL` | yes | — | AppFlowy-Cloud base URL, no trailing slash |
| `APPFLOWY_TLS_VERIFY` | no | `true` | Set `false` only for self-signed dev certs |
| `APPFLOWY_MCP_TRANSPORT` | no | `http` | Must be `http` — per-user auth requires the HTTP transport |
| `APPFLOWY_MCP_HOST` | no | `0.0.0.0` | Bind host |
| `APPFLOWY_MCP_PORT` | no | `8765` | Bind port |

No bot credentials. The server has no static identity; every request authenticates
under the user named in its `X-AppFlowy-Email` header.

---

## Development

Requires Python ≥ 3.10.

```bash
python -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env
# edit .env with your AppFlowy base URL and TLS settings

# Smoke-test the AppFlowy client directly (bypasses MCP layer; uses
# APPFLOWY_BOT_EMAIL/APPFLOWY_BOT_PASSWORD from env as the test user)
.venv/bin/python smoke_test.py

# Run the HTTP MCP server
.venv/bin/appflowy-mcp
```

### How it works

- **Per-user auth.** A small `ClientPool` in `server.py` reads
  `X-AppFlowy-Email` and `X-AppFlowy-Password` from each MCP request
  (`ctx.request_context.request.headers`) and looks up — or creates — an
  `AppFlowyClient` for that `(email, password)`. Each cached client logs in to
  AppFlowy under that user's identity and refreshes its own tokens
  independently.
- **Reads** decode the raw `encoded_collab` from `/page-view` using
  [pycrdt](https://github.com/y-crdt/pycrdt), then render the document tree to
  Markdown (server-side `/collab/json` flattens Y.Text into plain strings and
  loses inline formatting).
- **Writes** assemble a Y.Doc with pycrdt using AppFlowy's document schema
  (root `data` → `document` → `blocks` + `meta.{children_map,text_map}`),
  wrap the encoded update + state vector into AppFlowy's bincode
  `EncodedCollab` envelope, and PUT it to `/api/workspace/{ws}/collab/{obj}`
  as `encoded_collab_v1`.
- After login, the client hits `GET /api/user/verify/{access_token}` once —
  otherwise the user is invisible to AppFlowy's `af_user` table and
  `list_workspaces` comes back empty.

### Adding a new tool

After adding a tool to [src/appflowy_mcp/server.py](src/appflowy_mcp/server.py)
(remember to take `ctx: Context` as the first parameter and call
`pool.get(ctx)` to obtain the per-user client), rebuild the image and
**restart your agent's session** — MCP clients only fetch the tool list at
connect time.

---

## Limitations

- Credentials travel in plain HTTP headers on every request. **Always
  terminate TLS in front of the MCP endpoint** when exposing it beyond
  localhost.
- No delete or move tools. Intentional — destructive operations should be a
  separate opt-in.
- Database row data (Grid/Board/Calendar contents) is not exposed; only the
  schema is returned by `read_page`.
- Writes via `replace_page_content` go through a background DB upsert. If the
  page is open in someone's AppFlowy browser/desktop client at the time, the
  live WebSocket session may overwrite the change — close the page before
  writing.

## License

[MIT](LICENSE).
