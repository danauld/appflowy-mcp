import asyncio
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .client import AppFlowyClient
from .config import Config
from .doc_builder import build_document
from .markdown import render_document
from .markdown_to_blocks import parse as parse_markdown


# AppFlowy ViewLayout enum: 0=Document, 1=Grid, 2=Board, 3=Calendar, 4=Chat
_LAYOUT_NAMES = {0: "Document", 1: "Grid", 2: "Board", 3: "Calendar", 4: "Chat"}
_LAYOUT_NAME_TO_INT = {v: k for k, v in _LAYOUT_NAMES.items()}

# AppFlowy CollabType enum used by /collab/json endpoint.
_LAYOUT_TO_COLLAB_TYPE = {0: 0, 1: 1, 2: 1, 3: 1}  # Chat (4) has no doc-style collab


class _MissingCredentials(ValueError):
    pass


def _trim_folder_view(node: dict[str, Any]) -> dict[str, Any]:
    layout = node.get("layout")
    return {
        "view_id": node.get("view_id"),
        "name": node.get("name"),
        "layout": _LAYOUT_NAMES.get(layout, layout),
        "is_space": node.get("is_space", False),
        "icon": node.get("icon"),
        "children": [_trim_folder_view(c) for c in node.get("children") or []],
    }


class ClientPool:
    """Per-user AppFlowyClient cache, keyed by (email, password).

    Each MCP request carries X-AppFlowy-Email/Password headers identifying the
    end user. The pool gives every distinct (email, password) its own client,
    which logs in to AppFlowy under that identity and refreshes its own tokens.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._cache: dict[tuple[str, str], AppFlowyClient] = {}
        self._lock = asyncio.Lock()

    async def get(self, ctx: Context) -> AppFlowyClient:
        try:
            request = ctx.request_context.request
        except (AttributeError, LookupError, ValueError) as exc:
            raise _MissingCredentials(
                "MCP server requires HTTP transport with "
                "X-AppFlowy-Email/X-AppFlowy-Password headers"
            ) from exc
        if request is None or not hasattr(request, "headers"):
            raise _MissingCredentials(
                "No HTTP request context available; this server only supports "
                "the streamable-HTTP transport with per-user auth headers"
            )
        email = request.headers.get("X-AppFlowy-Email")
        password = request.headers.get("X-AppFlowy-Password")
        if not email or not password:
            raise _MissingCredentials(
                "Missing X-AppFlowy-Email or X-AppFlowy-Password header. "
                "Configure your MCP client to send both."
            )
        key = (email, password)
        async with self._lock:
            client = self._cache.get(key)
            if client is None:
                client = AppFlowyClient(
                    base_url=self._config.base_url,
                    email=email,
                    password=password,
                    verify=self._config.tls_verify,
                )
                self._cache[key] = client
        return client

    async def aclose(self) -> None:
        for client in self._cache.values():
            await client.aclose()
        self._cache.clear()


def build_server(config: Config) -> tuple[FastMCP, ClientPool]:
    mcp = FastMCP("appflowy", host=config.host, port=config.port)
    pool = ClientPool(config)

    @mcp.tool()
    async def list_workspaces(ctx: Context) -> list[dict[str, Any]]:
        """List AppFlowy workspaces accessible to the calling user.

        Returns one entry per workspace with: workspace_id, workspace_name,
        owner_email, role (Owner / Member / Guest), icon, created_at.
        """
        client = await pool.get(ctx)
        rows = await client.list_workspaces()
        return [
            {
                "workspace_id": r.get("workspace_id"),
                "workspace_name": r.get("workspace_name"),
                "owner_email": r.get("owner_email"),
                "role": r.get("role"),
                "icon": r.get("icon"),
                "created_at": r.get("created_at"),
            }
            for r in rows
        ]

    @mcp.tool()
    async def list_pages(
        ctx: Context, workspace_id: str, depth: int = 10
    ) -> dict[str, Any]:
        """Return the folder tree of a workspace.

        Each node has: view_id, name, layout (Document/Grid/Board/Calendar/Chat),
        is_space (top-level container like a Shared section), icon, children.

        Use the returned view_id with read_page() to fetch a page's content.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            depth: How deep to walk the tree. Default 10 covers most layouts.
        """
        client = await pool.get(ctx)
        folder = await client.get_folder(workspace_id, depth=depth)
        return _trim_folder_view(folder)

    @mcp.tool()
    async def read_page(
        ctx: Context, workspace_id: str, view_id: str
    ) -> dict[str, Any]:
        """Read a page's content as Markdown.

        For Document layouts: returns rendered Markdown reconstructed from the
        document's decoded CRDT (blocks + child ordering + text deltas).
        For Grid/Board/Calendar: returns the database schema as JSON
        (row data not yet exposed via this tool).
        For Chat or unknown layouts: returns an error.

        Args:
            workspace_id: Workspace UUID.
            view_id: Page UUID (from list_pages).
        """
        client = await pool.get(ctx)
        meta = await client.get_page_view(workspace_id, view_id)
        view_meta = meta.get("view") or {}
        layout = view_meta.get("layout")
        collab_type = _LAYOUT_TO_COLLAB_TYPE.get(layout)
        if collab_type is None:
            return {
                "view_id": view_id,
                "name": view_meta.get("name"),
                "layout": _LAYOUT_NAMES.get(layout, layout),
                "error": f"layout {layout!r} is not readable as a document",
            }
        result: dict[str, Any] = {
            "view_id": view_id,
            "name": view_meta.get("name"),
            "layout": _LAYOUT_NAMES.get(layout, layout),
            "owner_email": (meta.get("owner") or {}).get("email"),
            "last_editor_email": (meta.get("last_editor") or {}).get("email"),
        }
        if collab_type == 0:
            # Decode via pycrdt to preserve inline formatting deltas. The
            # server's /collab/json flattens Y.Text into plain strings.
            body = await client.get_document_decoded(workspace_id, view_id)
            result["content_markdown"] = render_document(body)
        else:
            body = await client.get_collab_json(workspace_id, view_id, collab_type)
            result["content_json"] = body
        return result

    @mcp.tool()
    async def create_page(
        ctx: Context,
        workspace_id: str,
        parent_view_id: str,
        name: str,
        layout: str = "Document",
    ) -> dict[str, Any]:
        """Create a new empty page in a workspace.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            parent_view_id: Parent view UUID. Use a *space* view_id (is_space=true
                in the folder tree) for a top-level page in that space, or
                another page's view_id to nest under it. The workspace root
                itself usually doesn't accept direct children — pick a space.
            name: Page name.
            layout: One of "Document", "Grid", "Board", "Calendar", "Chat".
                Default "Document".

        Returns: { view_id } of the newly created page. Use it with read_page()
        once the page has content, or with future edit tools.
        """
        layout_int = _LAYOUT_NAME_TO_INT.get(layout)
        if layout_int is None:
            return {
                "error": (
                    f"layout must be one of "
                    f"{sorted(_LAYOUT_NAME_TO_INT)}; got {layout!r}"
                )
            }
        client = await pool.get(ctx)
        page = await client.create_page(workspace_id, parent_view_id, name, layout_int)
        return {"view_id": page.get("view_id"), "name": name, "layout": layout}

    @mcp.tool()
    async def rename_page(
        ctx: Context, workspace_id: str, view_id: str, new_name: str
    ) -> dict[str, Any]:
        """Rename an existing page.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            view_id: Page UUID (from list_pages).
            new_name: New page name.

        Returns: { view_id, name } on success.
        """
        client = await pool.get(ctx)
        await client.rename_page(workspace_id, view_id, new_name)
        return {"view_id": view_id, "name": new_name}

    @mcp.tool()
    async def replace_page_content(
        ctx: Context, workspace_id: str, view_id: str, markdown_content: str
    ) -> dict[str, Any]:
        """Replace a Document page's entire content with new markdown.

        WARNING: This *replaces* the page body — anything that was there is lost.
        Read first with read_page() if you need to preserve / merge.

        IMPORTANT — live editor conflict: if the page is currently open in
        someone's AppFlowy browser/desktop client (WebSocket session active),
        the live client's local Y.Doc state will overwrite our write. Close all
        AppFlowy tabs/windows for this page before calling this tool, then
        reopen after to see the change. (The realtime-sync write path that
        avoids this is non-trivial; tracked separately.)

        Supported markdown:
        - Headings, paragraphs, bulleted/numbered/todo lists with nesting,
          quotes, fenced code, dividers, simple_table with alignments
        - Inline: **bold**, *italic*, `code`, ~~strike~~, [link](url)

        Only valid for Document-layout pages. Use create_page() to make one.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            view_id: Page UUID (from list_pages).
            markdown_content: The new page body as markdown.

        Returns: { view_id, blocks_written } on success.
        """
        client = await pool.get(ctx)
        blocks = parse_markdown(markdown_content)
        encoded = build_document(blocks)
        await client.update_page_collab(
            workspace_id, view_id, encoded, collab_type=0
        )
        return {"view_id": view_id, "blocks_written": len(blocks)}

    return mcp, pool
