# appflowy-mcp

An MCP server that lets LLM agents (Claude Code, Cline, Claude Desktop, ...) read
and write pages in a self-hosted [AppFlowy-Cloud](https://github.com/AppFlowy-IO/AppFlowy-Cloud)
instance.

The server talks to AppFlowy's HTTP API on behalf of a single bot account. Share
the workspaces and pages you want exposed with that bot — anything it can see,
agents can see.

## Tools

| Tool | Purpose |
|---|---|
| `list_workspaces()` | All workspaces the bot has access to |
| `list_pages(workspace_id, depth=10)` | Folder tree of a workspace |
| `read_page(workspace_id, view_id)` | Document content as Markdown (or Grid/Board schema as JSON) |
| `create_page(workspace_id, parent_view_id, name, layout="Document")` | Create an empty page |
| `rename_page(workspace_id, view_id, new_name)` | Rename a page |
| `replace_page_content(workspace_id, view_id, markdown_content)` | Replace a Document page's body with Markdown |

`replace_page_content` accepts headings, paragraphs, bulleted/numbered/todo lists
with indent-based nesting (2 spaces/level), block quotes, fenced code blocks, and
horizontal rules. Inline bold/italic/links pass through as plain text for now.

There are no delete/move tools — by design. Reads + additive edits only.

---

## For admins — deploying the server

The server is packaged as a Docker image and is designed to run next to
AppFlowy-Cloud (same Compose stack), exposing a streamable-HTTP MCP endpoint
that team members connect their agents to.

### 1. Create a bot account in AppFlowy

In AppFlowy-Cloud, create a regular user (e.g. via the GoTrue admin API or your
usual provisioning flow) and share with it every workspace/page you want agents
to access.

### 2. Add the service to your Compose stack

Copy [docker-compose.example.yml](docker-compose.example.yml) into your
AppFlowy-Cloud `docker-compose.yml` (or include it via an override file). Set
the bot credentials in your `.env`:

```env
APPFLOWY_MCP_BOT_EMAIL=appflowy-bot@your.domain
APPFLOWY_MCP_BOT_PASSWORD=...
```

Then:

```bash
docker compose up -d appflowy_mcp
docker compose logs -f appflowy_mcp
```

`APPFLOWY_BASE_URL` inside the container points at the internal `appflowy_cloud`
service, bypassing nginx/TLS. The MCP endpoint itself is plain HTTP on port
`8765` — terminate TLS at your reverse proxy if exposing beyond localhost.

### 3. Hand the endpoint URL to your team

Whatever `http(s)://host:8765/mcp` resolves to from the team's machines — that's
what every user puts in their agent config.

---

## For users — connecting your agent

You need **the URL** of the MCP server (from your admin) and an agent that
speaks MCP over HTTP.

### Claude Code

```bash
claude mcp add --transport http appflowy https://your-server:8765/mcp
claude mcp list   # appflowy ✓ Connected
```

Restart the Claude Code session so it picks up the tools.

### Claude Desktop / Cline / others

Add an HTTP MCP server pointing at the same URL. Consult your client's docs for
the exact JSON shape — the transport is "streamable HTTP" (the MCP spec
default for HTTP).

### Verifying

Ask your agent to call `list_workspaces` — you should see at least one
workspace, the one(s) the admin shared with the bot.

---

## Configuration

All settings come from environment variables (`.env` is loaded in dev mode for
convenience). See [.env.example](.env.example).

| Var | Required | Default | Notes |
|---|---|---|---|
| `APPFLOWY_BASE_URL` | yes | — | AppFlowy-Cloud base URL, no trailing slash |
| `APPFLOWY_BOT_EMAIL` | yes | — | Bot account email |
| `APPFLOWY_BOT_PASSWORD` | yes | — | Bot account password |
| `APPFLOWY_TLS_VERIFY` | no | `true` | Set `false` only for self-signed dev certs |
| `APPFLOWY_MCP_TRANSPORT` | no | `stdio` | `stdio` (local dev) or `http` (server) |
| `APPFLOWY_MCP_HOST` | no | `0.0.0.0` | Bind host, HTTP transport only |
| `APPFLOWY_MCP_PORT` | no | `8765` | Bind port, HTTP transport only |

---

## Development

Requires Python ≥ 3.10.

```bash
python -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env
# edit .env with your bot credentials

# Smoke-test the AppFlowy client directly (bypasses MCP layer)
.venv/bin/python smoke_test.py

# Run as a local stdio MCP server
APPFLOWY_MCP_TRANSPORT=stdio .venv/bin/appflowy-mcp

# Or run as an HTTP MCP server
APPFLOWY_MCP_TRANSPORT=http .venv/bin/appflowy-mcp
```

### How it works

- **Reads** go through `GET /api/workspace/v1/{ws}/collab/{view}/json` —
  AppFlowy-Cloud server-side decodes the Yrs CRDT and returns a JSON AST.
  No Yrs decoder runs on our side.
- **Writes** assemble a Y.Doc with [pycrdt](https://github.com/y-crdt/pycrdt)
  using AppFlowy's document schema (root `data` → `document` → `blocks` +
  `meta.{children_map,text_map}`), wrap the encoded update + state vector
  into AppFlowy's bincode `EncodedCollab` envelope, and PUT it to
  `/api/workspace/{ws}/collab/{obj}` as `encoded_collab_v1`.
- After login, the server hits `GET /api/user/verify/{access_token}` once —
  otherwise the bot is invisible to AppFlowy's `af_user` table and
  `list_workspaces` comes back empty.

### Adding a new tool

After adding a tool to [src/appflowy_mcp/server.py](src/appflowy_mcp/server.py),
rebuild the image and **restart your agent's session** — MCP clients only fetch
the tool list at connect time.

---

## Limitations

- One shared bot account, no per-user identity. Audit logs in AppFlowy show
  every action as that bot. If you need per-user auth, this isn't the project
  for you (yet).
- No inline formatting in `replace_page_content` — bold/italic/links/inline
  code come through as plain text. The block-level structure (headings, lists,
  code blocks, quotes) is preserved.
- No delete or move tools. Intentional — destructive operations should be a
  separate opt-in.
- Database row data (Grid/Board/Calendar contents) is not exposed; only the
  schema is returned by `read_page`.

## License

[MIT](LICENSE).
