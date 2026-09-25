import asyncio
import json as _json
import re
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .client import AppFlowyClient, AppFlowyError
from .config import Config
from .database_collab import (
    FIELD_TYPE_NAMES,
    extract_collab_cells,
    get_field_select_options,
    parse_relation_row_ids,
)
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


async def _resolve_document_view_id(
    client: AppFlowyClient, workspace_id: str, view_id: str
) -> str:
    """Resolve a view_id or database row_id to the document object_id."""
    try:
        meta = await client.get_page_view(workspace_id, view_id)
        if (meta.get("view") or {}).get("layout") == 0:
            return view_id
    except Exception:
        pass
    try:
        return client.row_to_document_id(view_id)
    except Exception:
        return view_id


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

        Accepts either a document page `view_id` OR a database card's `row_id`
        (automatically resolved to the card's body document).

        For Document layouts / card bodies: returns rendered Markdown reconstructed from the
        document's decoded CRDT (blocks + child ordering + text deltas).
        For Grid/Board/Calendar: returns the database schema as JSON
        (use get_database_fields / get_database_rows for columns + row data).
        For uninitialized card bodies: returns empty content_markdown.

        Args:
            workspace_id: Workspace UUID.
            view_id: Page UUID (from list_pages) or Database Row UUID (from get_database_rows).
        """
        client = await pool.get(ctx)
        meta: dict[str, Any] = {}
        target_view_id = view_id
        is_row_card = False
        try:
            meta = await client.get_page_view(workspace_id, view_id)
        except AppFlowyError:
            meta = {}

        view_meta = meta.get("view") or {}
        layout = view_meta.get("layout")

        # If layout is None, check if view_id is a database row ID
        if layout is None:
            try:
                row_doc_id = client.row_to_document_id(view_id)
                row_meta = await client.get_page_view(workspace_id, row_doc_id)
                row_view_meta = row_meta.get("view") or {}
                if row_view_meta.get("layout") is not None:
                    target_view_id = row_doc_id
                    meta = row_meta
                    view_meta = row_view_meta
                    layout = row_view_meta.get("layout")
                    is_row_card = True
                else:
                    body = await client.get_document_decoded(
                        workspace_id, row_doc_id
                    )
                    if body:
                        return {
                            "view_id": row_doc_id,
                            "row_id": view_id,
                            "name": view_meta.get("name") or "",
                            "layout": "Document",
                            "owner_email": (meta.get("owner") or {}).get(
                                "email"
                            ),
                            "last_editor_email": (
                                meta.get("last_editor") or {}
                            ).get("email"),
                            "content_markdown": render_document(body),
                        }
                    return {
                        "view_id": row_doc_id,
                        "row_id": view_id,
                        "name": view_meta.get("name") or "",
                        "layout": "Document",
                        "owner_email": (meta.get("owner") or {}).get("email"),
                        "last_editor_email": (
                            meta.get("last_editor") or {}
                        ).get("email"),
                        "content_markdown": "",
                        "empty": True,
                    }
            except Exception:
                pass

        collab_type = _LAYOUT_TO_COLLAB_TYPE.get(layout)
        if collab_type is None:
            return {
                "view_id": view_id,
                "name": view_meta.get("name"),
                "layout": _LAYOUT_NAMES.get(layout, layout),
                "error": f"layout {layout!r} is not readable as a document",
            }
        result: dict[str, Any] = {
            "view_id": target_view_id,
            "name": view_meta.get("name"),
            "layout": _LAYOUT_NAMES.get(layout, layout),
            "owner_email": (meta.get("owner") or {}).get("email"),
            "last_editor_email": (meta.get("last_editor") or {}).get("email"),
        }
        if is_row_card:
            result["row_id"] = view_id

        if collab_type == 0:
            # Decode via pycrdt to preserve inline formatting deltas. The
            # server's /collab/json flattens Y.Text into plain strings.
            body = await client.get_document_decoded(
                workspace_id, target_view_id
            )
            result["content_markdown"] = render_document(body)
        else:
            body = await client.get_collab_json(
                workspace_id, target_view_id, collab_type
            )
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

        Only valid for Document-layout pages or database card body documents.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            view_id: Page UUID (from list_pages) or Database Row UUID (from get_database_rows).
            markdown_content: The new page body as markdown.

        Returns: { view_id, blocks_written } on success.
        """
        client = await pool.get(ctx)
        target_id = await _resolve_document_view_id(client, workspace_id, view_id)
        blocks = parse_markdown(markdown_content)
        encoded = build_document(blocks)
        await client.update_page_collab(
            workspace_id, target_id, encoded, collab_type=0
        )
        return {"view_id": target_id, "blocks_written": len(blocks)}

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

        Only valid for Document-layout pages or database card body documents.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            view_id: Page UUID (from list_pages) or Database Row UUID (from get_database_rows).
            markdown_content: Markdown to append at the end of the page.

        Returns: { view_id, blocks_appended } on success.
        """
        new_blocks = parse_markdown(markdown_content)
        if not new_blocks:
            return {"view_id": view_id, "blocks_appended": 0}

        client = await pool.get(ctx)
        target_id = await _resolve_document_view_id(client, workspace_id, view_id)
        page: dict[str, Any] = {}
        try:
            page = await client.get_page_view(workspace_id, target_id)
        except AppFlowyError:
            pass
        raw = bytes(page.get("data", {}).get("encoded_collab") or page.get("encoded_collab") or b"")
        if not raw:
            try:
                cdata = await client.get_collab(workspace_id, target_id, collab_type=0)
                if cdata.get("doc_state"):
                    raw = bytes(cdata["doc_state"])
            except Exception:
                pass
        if not raw:
            encoded = build_document(new_blocks)
            await client.update_page_collab(
                workspace_id, target_id, encoded, collab_type=0
            )
            return {"view_id": target_id, "blocks_appended": len(new_blocks)}

        encoded = append_blocks_to_document(raw, new_blocks)
        await client.update_page_collab(
            workspace_id, target_id, encoded, collab_type=0
        )
        return {"view_id": target_id, "blocks_appended": len(new_blocks)}

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
            view_id: Page UUID or Database Row UUID.
            heading: Heading text to match (e.g. "Доступные MCP-tools").
            new_markdown: Markdown to put in place of the section.
            match_index: 0-based index for disambiguating multiple matches.
                Default None means "fail if ambiguous".

        Returns: { view_id, blocks_written, action: "replaced" } on success,
                 { view_id, error } on no/ambiguous match.
        """
        new_blocks = parse_markdown(new_markdown)

        client = await pool.get(ctx)
        target_id = await _resolve_document_view_id(client, workspace_id, view_id)
        page: dict[str, Any] = {}
        try:
            page = await client.get_page_view(workspace_id, target_id)
        except AppFlowyError:
            pass
        raw = bytes(page.get("data", {}).get("encoded_collab") or page.get("encoded_collab") or b"")
        if not raw:
            try:
                cdata = await client.get_collab(workspace_id, target_id, collab_type=0)
                if cdata.get("doc_state"):
                    raw = bytes(cdata["doc_state"])
            except Exception:
                pass
        if not raw:
            return {
                "view_id": target_id,
                "error": "page has no existing document",
            }

        encoded, err = replace_section_in_document(
            raw, heading, new_blocks, match_index
        )
        if err is not None:
            return {"view_id": target_id, "error": err}

        await client.update_page_collab(
            workspace_id, target_id, encoded, collab_type=0
        )
        return {
            "view_id": target_id,
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
            view_id: Page UUID or Database Row UUID.
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
        target_id = await _resolve_document_view_id(client, workspace_id, view_id)
        page: dict[str, Any] = {}
        try:
            page = await client.get_page_view(workspace_id, target_id)
        except AppFlowyError:
            pass
        raw = bytes(page.get("data", {}).get("encoded_collab") or page.get("encoded_collab") or b"")
        if not raw:
            try:
                cdata = await client.get_collab(workspace_id, target_id, collab_type=0)
                if cdata.get("doc_state"):
                    raw = bytes(cdata["doc_state"])
            except Exception:
                pass
        if not raw:
            return {
                "view_id": target_id,
                "error": "page has no existing document",
            }

        encoded, err = insert_after_heading_in_document(
            raw, heading, new_blocks, match_index
        )
        if err is not None:
            return {"view_id": target_id, "error": err}

        await client.update_page_collab(
            workspace_id, target_id, encoded, collab_type=0
        )
        return {
            "view_id": target_id,
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
            view_id: Page UUID or Database Row UUID.
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
        target_id = await _resolve_document_view_id(client, workspace_id, view_id)
        page: dict[str, Any] = {}
        try:
            page = await client.get_page_view(workspace_id, target_id)
        except AppFlowyError:
            pass
        raw = bytes(page.get("data", {}).get("encoded_collab") or page.get("encoded_collab") or b"")
        if not raw:
            try:
                cdata = await client.get_collab(workspace_id, target_id, collab_type=0)
                if cdata.get("doc_state"):
                    raw = bytes(cdata["doc_state"])
            except Exception:
                pass
        if not raw:
            return {
                "view_id": target_id,
                "error": "page has no existing document",
            }

        encoded, err = insert_before_heading_in_document(
            raw, heading, new_blocks, match_index
        )
        if err is not None:
            return {"view_id": target_id, "error": err}

        await client.update_page_collab(
            workspace_id, target_id, encoded, collab_type=0
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

    # ------------------------------------------------------------------ #
    # Database tools (Grid / Board / Calendar).                          #
    #                                                                    #
    # AppFlowy-Cloud lets you create a database *view* (create_page with #
    # layout="Grid"/"Board"/"Calendar"), read its fields, and read/write #
    # rows — but it has NO API to create or define fields/columns/select #
    # options. So these tools fill and read databases whose columns      #
    # already exist (a fresh Grid's default Name/Type/Done columns, or   #
    # columns you set up by hand in the AppFlowy app).                   #
    # ------------------------------------------------------------------ #

    @mcp.tool()
    async def list_databases(
        ctx: Context, workspace_id: str
    ) -> list[dict[str, Any]]:
        """List the databases (Grid/Board/Calendar) in a workspace.

        A database is the data object behind one or more Grid/Board/Calendar
        views. The database_id differs from the folder page's view_id: creating
        a Grid with create_page() makes a folder page whose child is the
        database view. Use the database_id (or any view_id) shown here with the
        other database tools.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).

        Returns: [{ database_id, views: [{view_id, name, layout}] }]
        """
        client = await pool.get(ctx)
        dbs = await client.list_databases(workspace_id)
        return [
            {
                "database_id": d.get("id"),
                "views": [
                    {
                        "view_id": v.get("view_id"),
                        "name": v.get("name"),
                        "layout": _LAYOUT_NAMES.get(
                            v.get("layout"), v.get("layout")
                        ),
                    }
                    for v in d.get("views") or []
                ],
            }
            for d in dbs
        ]

    @mcp.tool()
    async def get_database_fields(
        ctx: Context, workspace_id: str, database_ref: str
    ) -> dict[str, Any]:
        """Read a database's fields (columns), their IDs, types, and options.

        Call this before writing rows — row cells can be keyed by field name or
        field id. For SingleSelect/MultiSelect fields you can set values that
        exist in `options` (or add new ones with `add_select_option`).
        For Relation fields, `relation_database_id` shows the linked target database.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            database_ref: A database_id, a database view_id, or the folder
                view_id create_page() returned for the Grid/Board/Calendar.

        Returns: { database_id, fields: [{id, name, field_type, is_primary,
                 options?, relation_database_id?}] }
        """
        client = await pool.get(ctx)
        try:
            db = await client.resolve_database_id(workspace_id, database_ref)
        except AppFlowyError as exc:
            return {"error": str(exc)}
        fields = await client.get_database_fields(workspace_id, db)
        out: list[dict[str, Any]] = []
        for f in fields:
            entry: dict[str, Any] = {
                "id": f.get("id"),
                "name": f.get("name"),
                "field_type": FIELD_TYPE_NAMES.get(
                    f.get("field_type"), f.get("field_type")
                ),
                "is_primary": bool(f.get("is_primary")),
            }
            opts = get_field_select_options(f)
            if opts:
                entry["options"] = [
                    o.get("name") for o in opts if o.get("name")
                ]
            to = f.get("type_option") or {}
            target_db = to.get("database_id")
            if not target_db:
                content = to.get("content")
                if isinstance(content, str):
                    try:
                        c_dict = _json.loads(content)
                        if isinstance(c_dict, dict):
                            target_db = c_dict.get("database_id")
                    except Exception:
                        pass
                elif isinstance(content, dict):
                    target_db = content.get("database_id")
            if target_db:
                entry["relation_database_id"] = str(target_db)
            out.append(entry)
        return {"database_id": db, "fields": out}

    @mcp.tool()
    async def get_database_rows(
        ctx: Context,
        workspace_id: str,
        database_ref: str,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        with_doc: bool = False,
    ) -> dict[str, Any]:
        """Read rows from a database with pagination, search, doc bodies, and resolved relations.

        Cells come back keyed by field name:
        - Text / URL fields → string
        - Checkbox → true/false
        - SingleSelect → option name (or "" when unset)
        - MultiSelect → list of option names
        - Relation fields → list of linked records: [{ id, title }]
        - unset cells → null

        If `with_doc` is True, each row also includes `content_markdown` containing
        the row's detail document body (if any).

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            database_ref: database_id or a view_id (see list_databases).
            limit: Max rows to return. Default 100.
            offset: Number of rows to skip. Default 0.
            search: Optional substring to filter rows (matches against cell values or primary title).
            with_doc: If True, fetch and render each row's body document as markdown.

        Returns: { database_id, total, returned, offset, rows: [{id, cells, content_markdown?}] }
        """
        client = await pool.get(ctx)
        try:
            db = await client.resolve_database_id(workspace_id, database_ref)
        except AppFlowyError as exc:
            return {"error": str(exc)}

        fields = await client.get_database_fields(workspace_id, db)
        relation_fields = [
            f
            for f in fields
            if f.get("field_type") == 10
            or str(f.get("field_type")).lower() == "relation"
        ]

        ids = await client.get_database_row_ids(workspace_id, db)
        total = len(ids)

        if search:
            # When searching, fetch up to 500 rows to filter locally
            all_rows = await client.get_database_rows(
                workspace_id, db, ids[:500]
            )
            needle = search.lower().strip()
            matched = []
            for r in all_rows:
                cells = r.get("cells") or {}
                matched_row = False
                for v in cells.values():
                    if needle in str(v).lower():
                        matched_row = True
                        break
                if matched_row or needle in str(r.get("id", "")).lower():
                    matched.append(r)
            total = len(matched)
            selected_rows = matched[offset : offset + max(0, limit)]
        else:
            slice_ids = ids[offset : offset + max(0, limit)]
            selected_rows = await client.get_database_rows(
                workspace_id, db, slice_ids
            )

        # 1. Enrich Relation cells if relation fields exist
        if relation_fields and selected_rows:
            collab_tasks = [
                client.get_collab_json(workspace_id, r["id"], 4)
                for r in selected_rows
                if r.get("id")
            ]
            collab_results = await asyncio.gather(
                *collab_tasks, return_exceptions=True
            )

            db_to_row_ids: dict[str, set[str]] = {}
            row_to_relations: dict[str, dict[str, list[str]]] = {}

            for r, cdata in zip(selected_rows, collab_results):
                rid = r.get("id")
                if (
                    not rid
                    or isinstance(cdata, Exception)
                    or not isinstance(cdata, dict)
                ):
                    continue
                cells_map = extract_collab_cells(cdata)
                for rf in relation_fields:
                    fid = str(rf.get("id"))
                    fname = str(rf.get("name"))
                    cell_obj = cells_map.get(fid) or cells_map.get(fname)
                    if isinstance(cell_obj, dict):
                        raw_data = cell_obj.get("data")
                        linked_ids = parse_relation_row_ids(raw_data)
                        if linked_ids:
                            row_to_relations.setdefault(rid, {})[
                                fname
                            ] = linked_ids
                            target_db = rf.get("relation_database_id")
                            if not target_db:
                                to = rf.get("type_option") or {}
                                target_db = to.get("database_id")
                                if not target_db and "content" in to:
                                    cnt = to["content"]
                                    if isinstance(cnt, str):
                                        try:
                                            cnt = _json.loads(cnt)
                                        except Exception:
                                            cnt = {}
                                    if isinstance(cnt, dict):
                                        target_db = cnt.get("database_id")
                            if target_db:
                                db_to_row_ids.setdefault(
                                    str(target_db), set()
                                ).update(linked_ids)

            # Resolve linked row titles against target databases
            resolved_titles: dict[str, str] = {}
            for t_db, t_rids in db_to_row_ids.items():
                try:
                    t_fields = await client.get_database_fields(
                        workspace_id, t_db
                    )
                    t_prim = next(
                        (
                            f.get("name")
                            for f in t_fields
                            if f.get("is_primary")
                        ),
                        "Name",
                    )
                    t_rows = await client.get_database_rows(
                        workspace_id, t_db, list(t_rids)
                    )
                    for tr in t_rows:
                        tid = tr.get("id")
                        tcells = tr.get("cells") or {}
                        resolved_titles[tid] = str(
                            tcells.get(t_prim)
                            or tcells.get("Name")
                            or tcells.get("Description")
                            or tid
                        )
                except Exception:
                    pass

            # Populate relation field values in selected_rows cells
            for r in selected_rows:
                rid = r.get("id")
                cells = r.setdefault("cells", {})
                for rf in relation_fields:
                    fname = str(rf.get("name"))
                    linked_ids = row_to_relations.get(rid, {}).get(fname, [])
                    cells[fname] = [
                        {"id": lid, "title": resolved_titles.get(lid, lid)}
                        for lid in linked_ids
                    ]

        # 2. Enrich with document content if requested
        if with_doc and selected_rows:
            doc_tasks = [
                client.get_document_decoded(
                    workspace_id, client.row_to_document_id(r["id"])
                )
                for r in selected_rows
                if r.get("id")
            ]
            doc_results = await asyncio.gather(
                *doc_tasks, return_exceptions=True
            )
            for r, dbody in zip(selected_rows, doc_results):
                if isinstance(dbody, dict) and dbody:
                    r["content_markdown"] = render_document(dbody)
                else:
                    r["content_markdown"] = ""

        return {
            "database_id": db,
            "total": total,
            "returned": len(selected_rows),
            "offset": offset,
            "rows": [
                {
                    "id": r.get("id"),
                    "cells": r.get("cells"),
                    **(
                        {"content_markdown": r.get("content_markdown")}
                        if with_doc
                        else {}
                    ),
                }
                for r in selected_rows
            ],
        }

    @mcp.tool()
    async def create_database_row(
        ctx: Context,
        workspace_id: str,
        database_ref: str,
        cells: dict[str, Any],
    ) -> dict[str, Any]:
        """Append a new row to a database.

        `cells` maps field NAME → value (call get_database_fields first to see
        the names). Value encodings: text fields → string; Checkbox →
        true/false; Number → number or numeric string; SingleSelect/MultiSelect
        → an EXISTING option name (unknown options are silently dropped —
        use add_select_option to create options first). Omitted fields stay blank.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            database_ref: database_id or a view_id (see list_databases).
            cells: { "Field Name": value, ... }.

        Returns: { database_id, row_id } on success, { error } otherwise.
        """
        client = await pool.get(ctx)
        try:
            db = await client.resolve_database_id(workspace_id, database_ref)
        except AppFlowyError as exc:
            return {"error": str(exc)}
        row_id = await client.create_database_row(workspace_id, db, cells)
        return {"database_id": db, "row_id": row_id}

    @mcp.tool()
    async def upsert_database_row(
        ctx: Context,
        workspace_id: str,
        database_ref: str,
        pre_hash: str,
        cells: dict[str, Any],
    ) -> dict[str, Any]:
        """Idempotently insert-or-update a row keyed by `pre_hash`.

        The row id is derived from `pre_hash`, so calling again with the same
        `pre_hash` updates the SAME row instead of adding a duplicate. Cells
        merge: fields you omit on a later call keep their previous value. Ideal
        for syncing an external record into a grid (use a stable external id as
        the pre_hash). Same cell encoding and select-option limits as
        create_database_row.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            database_ref: database_id or a view_id (see list_databases).
            pre_hash: Stable key identifying the row (e.g. an external id).
            cells: { "Field Name": value, ... }.

        Returns: { database_id, row_id } on success, { error } otherwise.
        """
        client = await pool.get(ctx)
        try:
            db = await client.resolve_database_id(workspace_id, database_ref)
        except AppFlowyError as exc:
            return {"error": str(exc)}
        row_id = await client.upsert_database_row(
            workspace_id, db, pre_hash, cells
        )
        return {"database_id": db, "row_id": row_id}

    @mcp.tool()
    async def update_database_row(
        ctx: Context,
        workspace_id: str,
        database_ref: str,
        row_id: str,
        cells: dict[str, Any],
    ) -> dict[str, Any]:
        """Update cells on an existing database row by row ID.

        Works for any row, including rows created manually in the UI or via
        external sync. Only the specified fields are updated; omitted fields
        retain their current values.

        Field names and allowed values:
        - RichText / Text / URL: string
        - Number: integer, float, or numeric string
        - Checkbox: true / false (or "Yes" / "No")
        - SingleSelect: option name (must exist; use add_select_option if new)
        - MultiSelect: list of option names or comma-separated string
        - Relation: list of linked row UUIDs or comma-separated UUIDs
        - DateTime: ISO-8601 string or Unix timestamp integer

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            database_ref: database_id or view_id (see list_databases).
            row_id: UUID of the row to update.
            cells: { "Field Name": value, ... }.

        Returns: { database_id, row_id, updated_fields: [...] } on success.
        """
        client = await pool.get(ctx)
        try:
            db = await client.resolve_database_id(workspace_id, database_ref)
        except AppFlowyError as exc:
            return {"error": str(exc)}
        try:
            res = await client.update_database_row(
                workspace_id, db, row_id, cells
            )
            return res
        except Exception as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def add_select_option(
        ctx: Context,
        workspace_id: str,
        database_ref: str,
        field_ref: str,
        name: str,
        color: str = "Purple",
    ) -> dict[str, Any]:
        """Add a new option to a SingleSelect or MultiSelect field in a database.

        Idempotent by name: if an option with this name already exists in the
        field, its existing option_id is returned without creating a duplicate.

        Allowed colors:
        Purple, Pink, LightPink, Orange, Yellow, Lime, Green, Aqua, Blue,
        Cream, Mint, Sky, Lilac, Pearl, Sunset, Coral, Sapphire, Moss, Sand, Charcoal.

        Args:
            workspace_id: Workspace UUID (from list_workspaces).
            database_ref: database_id or view_id (see list_databases).
            field_ref: Field name or field UUID (from get_database_fields).
            name: Option name / label.
            color: Color name (default: "Purple").

        Returns: { database_id, field_name, option_id, name, color, existing }
        """
        client = await pool.get(ctx)
        try:
            db = await client.resolve_database_id(workspace_id, database_ref)
        except AppFlowyError as exc:
            return {"error": str(exc)}
        try:
            res = await client.add_select_option(
                workspace_id, db, field_ref, name, color
            )
            return res
        except Exception as exc:
            return {"error": str(exc)}

    return mcp, pool
