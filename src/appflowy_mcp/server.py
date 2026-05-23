import asyncio
import re
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .client import AppFlowyClient
from .config import Config
from .doc_builder import (
    append_blocks_to_document,
    build_document,
    insert_after_heading_in_document,
    insert_before_heading_in_document,
    replace_section_in_document,
)
from .markdown import extract_plain_text, render_document
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


def _collect_document_pages(
    node: dict[str, Any], path: list[str], out: list[dict[str, str]]
) -> None:
    """Flatten the folder tree to a list of Document-layout pages with a
    breadcrumb path. The workspace root (the top-level node) is skipped as it
    has no view content."""
    name = node.get("name") or ""
    is_root = not path and not name
    new_path = path if is_root else path + [name]
    if (
        node.get("layout") == 0
        and not node.get("is_space")
        and node.get("view_id")
    ):
        out.append(
            {
                "view_id": node["view_id"],
                "name": name,
                "path": " / ".join(new_path),
            }
        )
    for child in node.get("children") or []:
        _collect_document_pages(child, new_path, out)


def _find_matches(
    text: str, query: str, case_sensitive: bool, use_regex: bool
) -> tuple[int, int, int] | None:
    """Return (first_start, first_end, total_count) or None if no match."""
    if not text or not query:
        return None
    if use_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query, flags)
        except re.error:
            return None
        matches = list(pattern.finditer(text))
        if not matches:
            return None
        m = matches[0]
        return (m.start(), m.end(), len(matches))
    haystack = text if case_sensitive else text.lower()
    needle = query if case_sensitive else query.lower()
    first = haystack.find(needle)
    if first < 0:
        return None
    count = 0
    pos = 0
    nlen = len(needle)
    while True:
        j = haystack.find(needle, pos)
        if j < 0:
            break
        count += 1
        pos = j + nlen
    return (first, first + nlen, count)


_WHITESPACE_RE = re.compile(r"\s+")


def _find_parent_and_siblings(
    node: dict[str, Any], target: str
) -> tuple[str, list[str]] | None:
    """Locate `target` view_id in the folder tree.

    Returns (parent_view_id, sibling_view_ids_in_order) where the siblings
    list includes the target itself. Returns None if not found.
    """
    children = node.get("children") or []
    for child in children:
        if child.get("view_id") == target:
            siblings = [c["view_id"] for c in children if c.get("view_id")]
            return node.get("view_id"), siblings
        found = _find_parent_and_siblings(child, target)
        if found is not None:
            return found
    return None


def _find_node(node: dict[str, Any], target: str) -> dict[str, Any] | None:
    if node.get("view_id") == target:
        return node
    for child in node.get("children") or []:
        found = _find_node(child, target)
        if found is not None:
            return found
    return None


def _resolve_prev_view_id(
    siblings: list[str], moving_view_id: str, position: str
) -> tuple[str | None, str | None]:
    """Translate a position spec into the `prev_view_id` AppFlowy expects.

    Siblings should be the children of the destination parent IN CURRENT ORDER.
    If the moving page is already among them, it is treated as removed first.

    Position spec:
      - "top"             → first child (prev_view_id = None)
      - "bottom"          → last child
      - "after:<id>"      → directly after the named sibling
      - "before:<id>"     → directly before the named sibling

    Returns (prev_view_id, error). Exactly one is None.
    """
    others = [s for s in siblings if s != moving_view_id]
    pos = position.strip()
    if pos == "top":
        return None, None
    if pos == "bottom":
        return (others[-1] if others else None), None
    if ":" in pos:
        kind, _, anchor = pos.partition(":")
        kind = kind.strip().lower()
        anchor = anchor.strip()
        if kind not in ("after", "before"):
            return None, f"unknown position kind {kind!r}; use 'after:' or 'before:'"
        if not anchor:
            return None, f"position {position!r} is missing the anchor view_id"
        if anchor == moving_view_id:
            return None, f"position references the moving page itself ({anchor})"
        if anchor not in others:
            return (
                None,
                f"anchor view_id {anchor} is not a sibling under the destination parent",
            )
        if kind == "after":
            return anchor, None
        idx = others.index(anchor)
        return (others[idx - 1] if idx > 0 else None), None
    return None, (
        f"unknown position {position!r}; "
        "expected one of 'top', 'bottom', 'after:<view_id>', 'before:<view_id>'"
    )


def _make_snippet(text: str, start: int, end: int, snippet_chars: int) -> str:
    half = max(20, snippet_chars // 2)
    left = max(0, start - half)
    right = min(len(text), end + half)
    snippet = _WHITESPACE_RE.sub(" ", text[left:right]).strip()
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


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
    async def reorder_page(
        ctx: Context,
        workspace_id: str,
        view_id: str,
        position: str,
    ) -> dict[str, Any]:
        """Reorder a page within its current parent (same section).

        Use this to pin a page to the top of its section, drop it to the bottom,
        or slot it next to a specific sibling. To change the parent itself
        (move across sections), use `move_page` instead.

        Position syntax:
          - "top"              — first among siblings
          - "bottom"           — last among siblings
          - "after:<view_id>"  — directly after the named sibling
          - "before:<view_id>" — directly before the named sibling

        Anchor view_ids for "after:" / "before:" must be siblings under the
        same parent. Call `list_pages` to look them up.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            view_id: Page UUID (from list_pages) — the page to move.
            position: One of the strings above.

        Returns: { view_id, parent_view_id, position, prev_view_id } on success,
                 { error } on bad position / page not found.
        """
        client = await pool.get(ctx)
        folder = await client.get_folder(workspace_id, depth=10)
        found = _find_parent_and_siblings(folder, view_id)
        if found is None:
            return {"error": f"view_id {view_id} not found in workspace folder tree"}
        parent_view_id, siblings = found
        if parent_view_id is None:
            return {"error": f"view_id {view_id} has no parent (is it the workspace root?)"}
        prev_view_id, err = _resolve_prev_view_id(siblings, view_id, position)
        if err is not None:
            return {"error": err}
        await client.move_page(workspace_id, view_id, parent_view_id, prev_view_id)
        return {
            "view_id": view_id,
            "parent_view_id": parent_view_id,
            "position": position,
            "prev_view_id": prev_view_id,
        }

    @mcp.tool()
    async def move_page(
        ctx: Context,
        workspace_id: str,
        view_id: str,
        new_parent_view_id: str,
        position: str = "top",
    ) -> dict[str, Any]:
        """Move a page under a different parent (cross-section move).

        Use this to relocate a page into another space or under another page.
        To merely reorder within the current parent, use `reorder_page`.

        Position syntax (same as `reorder_page`, resolved against the *new*
        parent's children):
          - "top" (default)    — first child of the new parent
          - "bottom"           — last child of the new parent
          - "after:<view_id>"  — directly after the named sibling
          - "before:<view_id>" — directly before the named sibling

        The server rejects moves into a page's own descendant (would create a
        cycle); the error is surfaced as-is.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            view_id: Page UUID — the page to move.
            new_parent_view_id: Destination parent's view_id. Use a space view_id
                for a top-level slot in that space, or another page's view_id
                to nest under it.
            position: Where to place the page under the new parent. Default "top".

        Returns: { view_id, new_parent_view_id, position, prev_view_id } on
                 success, { error } on bad position / page not found.
        """
        client = await pool.get(ctx)
        folder = await client.get_folder(workspace_id, depth=10)
        parent_node = _find_node(folder, new_parent_view_id)
        if parent_node is None:
            return {
                "error": f"new_parent_view_id {new_parent_view_id} not found in workspace folder tree"
            }
        if _find_node(folder, view_id) is None:
            return {"error": f"view_id {view_id} not found in workspace folder tree"}
        siblings = [
            c["view_id"]
            for c in (parent_node.get("children") or [])
            if c.get("view_id")
        ]
        prev_view_id, err = _resolve_prev_view_id(siblings, view_id, position)
        if err is not None:
            return {"error": err}
        await client.move_page(workspace_id, view_id, new_parent_view_id, prev_view_id)
        return {
            "view_id": view_id,
            "new_parent_view_id": new_parent_view_id,
            "position": position,
            "prev_view_id": prev_view_id,
        }

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

    @mcp.tool()
    async def append_to_page(
        ctx: Context, workspace_id: str, view_id: str, markdown_content: str
    ) -> dict[str, Any]:
        """Append markdown to the end of a Document page (no overwrite).

        Unlike replace_page_content which rewrites the whole page, this loads
        the existing Y.Doc, mutates it by inserting the new blocks at the end
        of the root page's children, and writes the updated full state back.
        Existing content (including formatting and inline marks) is preserved
        exactly as-is.

        Same live-editor conflict as replace_page_content: if the page is open
        in someone's AppFlowy browser/desktop client, the live WebSocket
        session can overwrite our write on its next sync. Close all editor
        tabs/windows for the page first, then reopen after.

        Supported markdown is the same set as replace_page_content (headings,
        paragraphs, lists with nesting, quotes, code, dividers, tables,
        inline **bold** / *italic* / `code` / [link](url) / ~~strike~~).

        Only valid for Document-layout pages.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            view_id: Page UUID (from list_pages).
            markdown_content: Markdown to append at the end of the page.

        Returns: { view_id, blocks_appended } on success.
        """
        new_blocks = parse_markdown(markdown_content)
        if not new_blocks:
            return {"view_id": view_id, "blocks_appended": 0}

        client = await pool.get(ctx)
        page = await client.get_page_view(workspace_id, view_id)
        raw = bytes(page.get("data", {}).get("encoded_collab") or b"")
        if not raw:
            return {
                "view_id": view_id,
                "blocks_appended": 0,
                "error": "page has no existing document; use replace_page_content first",
            }

        encoded = append_blocks_to_document(raw, new_blocks)
        await client.update_page_collab(
            workspace_id, view_id, encoded, collab_type=0
        )
        return {"view_id": view_id, "blocks_appended": len(new_blocks)}

    @mcp.tool()
    async def replace_section(
        ctx: Context,
        workspace_id: str,
        view_id: str,
        heading: str,
        new_markdown: str,
        match_index: int | None = None,
    ) -> dict[str, Any]:
        """Replace one section of a Document page (heading + body) with new markdown.

        A "section" is the heading itself plus every following root-level
        block until the next heading at the same-or-higher level (or the end
        of the page).

        Heading matching is case-insensitive and whitespace-normalized. If
        multiple root-level headings match the same text, the call fails with
        an error unless `match_index` is supplied (0-based).

        `new_markdown` is the full replacement content for the section. If it
        starts with a heading at the same level, that becomes the new section
        title; if not, the heading is removed along with the body. Pass an
        empty string to delete the section entirely.

        Same live-editor conflict as the other write tools — close all editor
        tabs/windows for the page before calling.

        Args:
            workspace_id: Workspace UUID.
            view_id: Page UUID.
            heading: Heading text to match (e.g. "Доступные MCP-tools").
            new_markdown: Markdown to put in place of the section.
            match_index: 0-based index for disambiguating multiple matches.
                Default None means "fail if ambiguous".

        Returns: { view_id, blocks_written, action: "replaced" } on success,
                 { view_id, error } on no/ambiguous match.
        """
        new_blocks = parse_markdown(new_markdown)

        client = await pool.get(ctx)
        page = await client.get_page_view(workspace_id, view_id)
        raw = bytes(page.get("data", {}).get("encoded_collab") or b"")
        if not raw:
            return {
                "view_id": view_id,
                "error": "page has no existing document",
            }

        encoded, err = replace_section_in_document(
            raw, heading, new_blocks, match_index
        )
        if err is not None:
            return {"view_id": view_id, "error": err}

        await client.update_page_collab(
            workspace_id, view_id, encoded, collab_type=0
        )
        return {
            "view_id": view_id,
            "blocks_written": len(new_blocks),
            "action": "replaced",
        }

    @mcp.tool()
    async def insert_after_heading(
        ctx: Context,
        workspace_id: str,
        view_id: str,
        heading: str,
        markdown_content: str,
        match_index: int | None = None,
    ) -> dict[str, Any]:
        """Insert markdown immediately after a root-level heading (top of section).

        Same matching/ambiguity rules as `replace_section`: case-insensitive,
        whitespace-normalized, multiple matches require `match_index`.

        Existing section body is preserved — new blocks go between the
        heading and whatever was its first body block.

        Same live-editor conflict as the other write tools.

        Args:
            workspace_id: Workspace UUID.
            view_id: Page UUID.
            heading: Heading text to insert after.
            markdown_content: Markdown to insert.
            match_index: 0-based index for disambiguating multiple matches.

        Returns: { view_id, blocks_written, action: "inserted" } on success,
                 { view_id, error } on no/ambiguous match.
        """
        new_blocks = parse_markdown(markdown_content)
        if not new_blocks:
            return {"view_id": view_id, "blocks_written": 0, "action": "inserted"}

        client = await pool.get(ctx)
        page = await client.get_page_view(workspace_id, view_id)
        raw = bytes(page.get("data", {}).get("encoded_collab") or b"")
        if not raw:
            return {
                "view_id": view_id,
                "error": "page has no existing document",
            }

        encoded, err = insert_after_heading_in_document(
            raw, heading, new_blocks, match_index
        )
        if err is not None:
            return {"view_id": view_id, "error": err}

        await client.update_page_collab(
            workspace_id, view_id, encoded, collab_type=0
        )
        return {
            "view_id": view_id,
            "blocks_written": len(new_blocks),
            "action": "inserted",
        }

    @mcp.tool()
    async def insert_before_heading(
        ctx: Context,
        workspace_id: str,
        view_id: str,
        heading: str,
        markdown_content: str,
        match_index: int | None = None,
    ) -> dict[str, Any]:
        """Insert markdown immediately before a root-level heading.

        New blocks go in front of the matched heading — i.e. at the end of
        the previous section, or at the very top of the page if the heading
        is the first block. Useful for placing a new H2 section ahead of an
        existing one without rewriting surrounding content.

        Same matching/ambiguity rules as `replace_section`: case-insensitive,
        whitespace-normalized, multiple matches require `match_index`.

        Same live-editor conflict as the other write tools.

        Args:
            workspace_id: Workspace UUID.
            view_id: Page UUID.
            heading: Heading text to insert before.
            markdown_content: Markdown to insert.
            match_index: 0-based index for disambiguating multiple matches.

        Returns: { view_id, blocks_written, action: "inserted" } on success,
                 { view_id, error } on no/ambiguous match.
        """
        new_blocks = parse_markdown(markdown_content)
        if not new_blocks:
            return {"view_id": view_id, "blocks_written": 0, "action": "inserted"}

        client = await pool.get(ctx)
        page = await client.get_page_view(workspace_id, view_id)
        raw = bytes(page.get("data", {}).get("encoded_collab") or b"")
        if not raw:
            return {
                "view_id": view_id,
                "error": "page has no existing document",
            }

        encoded, err = insert_before_heading_in_document(
            raw, heading, new_blocks, match_index
        )
        if err is not None:
            return {"view_id": view_id, "error": err}

        await client.update_page_collab(
            workspace_id, view_id, encoded, collab_type=0
        )
        return {
            "view_id": view_id,
            "blocks_written": len(new_blocks),
            "action": "inserted",
        }

    @mcp.tool()
    async def search_pages(
        ctx: Context,
        workspace_id: str,
        query: str,
        max_results: int = 20,
        case_sensitive: bool = False,
        use_regex: bool = False,
        snippet_chars: int = 200,
    ) -> dict[str, Any]:
        """Search Document-page contents in a workspace for a query string.

        Walks every Document-layout page reachable in the folder tree, scans its
        plain text (stripped of markdown formatting), and returns short snippets
        around the first match for each hit page. Pages with no match are
        omitted. Results are sorted by match_count descending.

        Use this to locate relevant pages cheaply, then call read_page() on the
        view_ids that look most promising. The server walks all pages
        internally; only snippets travel back — full page bodies stay on the
        server until you ask for them.

        Tips:
        - Refine queries iteratively: a broad term gives many hits, then narrow.
        - Set use_regex=True for alternations like "auth(orize|enticate)".
        - Increase snippet_chars if you need more surrounding context.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            query: Substring (default) or Python regex pattern. Empty string
                returns no matches.
            max_results: Cap on number of hit pages returned. Default 20.
            case_sensitive: Default false.
            use_regex: Treat query as a Python regex. Default false. Invalid
                regex returns an error field, not raises.
            snippet_chars: Approximate snippet width per match (split before
                and after the match). Default 200.

        Returns: {
            query, workspace_id, total_pages_scanned, total_matches,
            matches: [{view_id, name, path, snippet, match_count}, ...],
            error?: str,  # only on invalid regex
        }
        """
        if use_regex:
            try:
                re.compile(query)
            except re.error as exc:
                return {
                    "query": query,
                    "workspace_id": workspace_id,
                    "total_pages_scanned": 0,
                    "total_matches": 0,
                    "matches": [],
                    "error": f"invalid regex: {exc}",
                }

        client = await pool.get(ctx)
        folder = await client.get_folder(workspace_id, depth=10)
        pages: list[dict[str, str]] = []
        _collect_document_pages(folder, [], pages)

        semaphore = asyncio.Semaphore(8)

        async def scan(info: dict[str, str]) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    decoded = await client.get_document_decoded(
                        workspace_id, info["view_id"]
                    )
                except Exception:
                    return None
            text = extract_plain_text(decoded)
            if not text:
                return None
            hit = _find_matches(text, query, case_sensitive, use_regex)
            if hit is None:
                return None
            start, end, count = hit
            return {
                "view_id": info["view_id"],
                "name": info["name"],
                "path": info["path"],
                "snippet": _make_snippet(text, start, end, snippet_chars),
                "match_count": count,
            }

        results = await asyncio.gather(*(scan(p) for p in pages))
        matches = [r for r in results if r is not None]
        matches.sort(key=lambda r: r["match_count"], reverse=True)
        return {
            "query": query,
            "workspace_id": workspace_id,
            "total_pages_scanned": len(pages),
            "total_matches": len(matches),
            "matches": matches[: max(0, max_results)],
        }

    return mcp, pool
