"""Build an AppFlowy Y.Doc binary from a list of blocks (the inverse of markdown.py).

Schema (verified against AppFlowy-Collab e59260e):
    Doc.data (Map):
      "document" (Map):
        "page_id" → str
        "blocks" (Map):
          <block_id> (Map) { id, ty, parent, children, data(JSON str), external_id, external_type }
        "meta" (Map):
          "children_map" (Map) { <key> → Array<block_id> }
          "text_map"     (Map) { <key> → Text(plain str or with deltas) }

`build_document` returns bincode-serialized `EncodedCollab { state_vector,
doc_state, version=V1 }` for `PUT /api/workspace/{ws}/collab/{object_id}` (a new
document). Every edit of an EXISTING document returns a `DocEdit`: the incremental
Yrs v1 update for `POST .../collab/{obj}/web-update` (AppFlowy's realtime channel,
which applies the change live in open editors and only ships the delta) plus the
full state as a PUT fallback.
"""
import json
import struct
import uuid
from dataclasses import dataclass
from typing import Any, NamedTuple

from pycrdt import Array, Doc, Map, Text

from .inline import has_formatting, parse_inline


def _new_key(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _encode_encoded_collab(state_vector: bytes, doc_state: bytes, version: int = 0) -> bytes:
    """bincode 1.x default config: 8-byte LE length prefix, then bytes, then 1-byte enum tag."""
    out = struct.pack("<Q", len(state_vector)) + state_vector
    out += struct.pack("<Q", len(doc_state)) + doc_state
    out += struct.pack("<B", version)
    return out


def _add_block(
    blocks_map: Map,
    children_map: Map,
    text_map: Map,
    parent_id: str,
    block: dict[str, Any],
) -> str:
    """Insert one block (and its sub-tree) into the Y.Doc maps. Returns its id."""
    block_id = block["id"]
    children_key = _new_key("ch")
    has_text = block.get("text") is not None
    text_key = _new_key("txt") if has_text else ""

    # Build the block Map. Only include external_id/external_type for blocks
    # that actually carry text. AppFlowy treats empty-string external_id as
    # "has text" and tries to render it.
    block_fields: dict[str, Any] = {
        "id": block_id,
        "ty": block["ty"],
        "parent": parent_id,
        "children": children_key,
        "data": json.dumps(block.get("data") or {}),
    }
    if has_text:
        block_fields["external_id"] = text_key
        block_fields["external_type"] = "text"
    blocks_map[block_id] = Map(block_fields)

    # Children ordering: build first, then insert into children_map
    child_ids: list[str] = []
    for child in block.get("children") or []:
        cid = _add_block(blocks_map, children_map, text_map, block_id, child)
        child_ids.append(cid)
    children_map[children_key] = Array(child_ids)

    if text_key:
        raw_text = block.get("text") or ""
        runs = parse_inline(raw_text)
        if has_formatting(runs):
            # Insert plain text first, then format ranges. Insert-with-attrs
            # would inherit formatting onto neighboring runs in Yrs semantics.
            # IMPORTANT: pycrdt Text.format uses UTF-8 BYTE offsets, not chars.
            # For Cyrillic / emoji this matters (1 char ≠ 1 byte).
            text_map[text_key] = Text("")
            t = text_map[text_key]
            plain = "".join(chunk for chunk, _ in runs)
            t.insert(0, plain)
            offset = 0
            for chunk, attrs in runs:
                chunk_bytes = len(chunk.encode("utf-8"))
                if attrs:
                    t.format(offset, offset + chunk_bytes, attrs)
                offset += chunk_bytes
        else:
            text_map[text_key] = Text(raw_text)

    return block_id


def _populate_document(document_map: Map, blocks: list[dict[str, Any]]) -> None:
    """Fill an empty `document` Y.Map with AppFlowy document structure."""
    page_id = _new_key("page")
    page_children_key = _new_key("ch")
    document_map["page_id"] = page_id

    blocks_map = Map({})
    document_map["blocks"] = blocks_map
    # Root `page` block — no text, so omit external_id/external_type.
    blocks_map[page_id] = Map({
        "id": page_id,
        "ty": "page",
        "parent": "",
        "children": page_children_key,
        "data": "{}",
    })

    meta = Map({})
    document_map["meta"] = meta
    children_map = Map({})
    meta["children_map"] = children_map
    text_map = Map({})
    meta["text_map"] = text_map

    top_ids: list[str] = []
    for b in blocks:
        bid = _add_block(blocks_map, children_map, text_map, page_id, b)
        top_ids.append(bid)
    children_map[page_children_key] = Array(top_ids)


def build_document(blocks: list[dict[str, Any]]) -> bytes:
    """Build a complete AppFlowy document Y.Doc and serialize to bincode bytes
    (for `PUT /api/workspace/{ws}/collab/{obj}` — background DB upsert)."""
    doc = Doc()
    data_map = Map({})
    doc["data"] = data_map
    document = Map({})
    data_map["document"] = document
    _populate_document(document, blocks)

    doc_state = doc.get_update()
    state_vector = doc.get_state()
    return _encode_encoded_collab(state_vector, doc_state, version=0)


def _block_plain_text(
    block_id: str, blocks_map: Map, text_map: Map
) -> str:
    """Plain-text contents of a block (formatting stripped).

    Used for heading matching. Reads the block's `external_id` → corresponding
    Y.Text in text_map → concat of all insert chunks (regardless of attrs).
    """
    if block_id not in blocks_map:
        return ""
    block = blocks_map[block_id]
    if "external_id" not in list(block.keys()):
        return ""
    ext_id = block["external_id"]
    if not ext_id or ext_id not in text_map:
        return ""
    runs = text_map[ext_id].diff()
    return "".join(chunk for chunk, _attrs in runs)


def _block_data_dict(block: Map) -> dict[str, Any]:
    if "data" not in list(block.keys()):
        return {}
    raw = block["data"]
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_heading(s: str) -> str:
    return " ".join(s.split()).lower()


def _find_root_headings(
    blocks_map: Map, text_map: Map, root_children: Array, target: str
) -> list[tuple[int, str, int]]:
    """Return all root-level heading blocks matching `target` (case-insensitive,
    whitespace-normalized). Each entry: (index_in_root_children, block_id, level).
    """
    target_norm = _normalize_heading(target)
    out: list[tuple[int, str, int]] = []
    for i, bid in enumerate(list(root_children)):
        if bid not in blocks_map:
            continue
        block = blocks_map[bid]
        if block["ty"] != "heading":
            continue
        text = _block_plain_text(bid, blocks_map, text_map)
        if _normalize_heading(text) == target_norm:
            level = int(_block_data_dict(block).get("level", 1))
            out.append((i, bid, level))
    return out


def _section_end_index(
    blocks_map: Map, root_children: Array, start_index: int, heading_level: int
) -> int:
    """Index of the next root block that ends the section: a heading at level
    ≤ `heading_level`. Returns len(root_children) if no such block.
    """
    children = list(root_children)
    n = len(children)
    for i in range(start_index + 1, n):
        bid = children[i]
        if bid not in blocks_map:
            continue
        block = blocks_map[bid]
        if block["ty"] != "heading":
            continue
        level = int(_block_data_dict(block).get("level", 1))
        if level <= heading_level:
            return i
    return n


def _delete_block_tree(
    block_id: str, blocks_map: Map, children_map: Map, text_map: Map
) -> None:
    """Recursively remove a block and its descendants from all three maps."""
    if block_id not in blocks_map:
        return
    block = blocks_map[block_id]
    keys = list(block.keys())
    if "children" in keys:
        children_key = block["children"]
        if children_key and children_key in list(children_map.keys()):
            for cid in list(children_map[children_key]):
                _delete_block_tree(cid, blocks_map, children_map, text_map)
            del children_map[children_key]
    if "external_id" in keys:
        ext_id = block["external_id"]
        if ext_id and ext_id in list(text_map.keys()):
            del text_map[ext_id]
    del blocks_map[block_id]


def _resolve_match(
    matches: list[tuple[int, str, int]],
    heading: str,
    match_index: int | None,
) -> tuple[tuple[int, str, int] | None, str | None]:
    if not matches:
        return None, f"heading not found: {heading!r}"
    if match_index is None:
        if len(matches) > 1:
            return None, (
                f"multiple matches ({len(matches)}) for heading {heading!r}; "
                "specify match_index (0-based)"
            )
        return matches[0], None
    if match_index < 0 or match_index >= len(matches):
        return None, (
            f"match_index={match_index} out of range; {len(matches)} matches "
            f"found for heading {heading!r}"
        )
    return matches[match_index], None


class DocEdit:
    """One edit to an existing document, in both wire formats.

    `update` is the incremental Yrs v1 update from the server's state to the
    edited state. It is the body for `POST .../collab/{obj}/web-update`: the
    realtime server merges it and broadcasts it, so open editors show the change
    at once and cannot overwrite it, and only the delta travels, so page size is
    no limit. Proven live 2026-10-05 (appended block visible in an open AppFlowy
    window in ~2 s, still present after the editor's own sync).

    `encoded_v1` is the full edited state as a bincode `EncodedCollab`, for
    `PUT /collab/{obj}` when the realtime path is unavailable. That route is a
    background upsert capped at 5 MB, and a live editor's next sync can overwrite
    it, so it is the fallback, not the default.
    """

    def __init__(self, update: bytes, encoded_v1: bytes, blocks_written: int) -> None:
        self.update = update
        self.encoded_v1 = encoded_v1
        self.blocks_written = blocks_written


class _OpenDoc(NamedTuple):
    doc: Doc
    state_vector: bytes
    page_id: str
    blocks_map: Map
    children_map: Map
    text_map: Map
    root_children: Array


def _open_document(existing_encoded_collab: bytes) -> _OpenDoc:
    """Load the server's document state and remember where it stands, so the
    edit can be exported as a delta from exactly that state."""
    doc = Doc()
    doc.apply_update(existing_encoded_collab)
    state_vector = doc.get_state()
    document = doc.get("data", type=Map)["document"]
    page_id = document["page_id"]
    blocks_map = document["blocks"]
    meta = document["meta"]
    children_map = meta["children_map"]
    text_map = meta["text_map"]
    root_children = children_map[blocks_map[page_id]["children"]]
    return _OpenDoc(doc, state_vector, page_id, blocks_map, children_map, text_map, root_children)


def _finish(o: _OpenDoc, blocks_written: int) -> DocEdit:
    update = o.doc.get_update(o.state_vector)
    full = _encode_encoded_collab(o.doc.get_state(), o.doc.get_update(), version=0)
    return DocEdit(update, full, blocks_written)


def _insert_root_blocks(o: _OpenDoc, blocks: list[dict[str, Any]], at: int) -> None:
    new_ids = [
        _add_block(o.blocks_map, o.children_map, o.text_map, o.page_id, b)
        for b in blocks
    ]
    for offset, nid in enumerate(new_ids):
        o.root_children.insert(at + offset, nid)


def _remove_root_range(o: _OpenDoc, start: int, end: int) -> None:
    """Drop root children [start, end) and free their block trees. pycrdt's
    Y.Array has no slice deletion and indices shift, so delete `start`
    repeatedly."""
    doomed = list(o.root_children)[start:end]
    for _ in range(end - start):
        del o.root_children[start]
    for bid in doomed:
        _delete_block_tree(bid, o.blocks_map, o.children_map, o.text_map)


def replace_content_in_document(
    existing_encoded_collab: bytes, blocks: list[dict[str, Any]]
) -> DocEdit:
    """Replace everything under the root page with `blocks`, keeping the page
    block and the document's identity so the edit is a delta the realtime
    server can apply (unlike re-creating the `document` key, which the
    0.7-era attempt did and which open editors never picked up)."""
    o = _open_document(existing_encoded_collab)
    with o.doc.transaction():
        _remove_root_range(o, 0, len(o.root_children))
        _insert_root_blocks(o, blocks, 0)
    return _finish(o, len(blocks))


def replace_section_in_document(
    existing_encoded_collab: bytes,
    heading: str,
    new_blocks: list[dict[str, Any]],
    match_index: int | None = None,
) -> tuple[DocEdit | None, str | None]:
    """Replace one root-level section (heading + everything until the next
    same-or-higher heading) with new blocks. Returns (edit, error).

    Matching is case-insensitive and whitespace-normalized. If multiple
    root-level headings match the same text, `match_index` must be specified
    or the call returns an error without writing.

    `new_blocks` is the parsed markdown for the replacement — if it starts
    with a heading at the section level you're replacing, that becomes the
    new section header; if not, the heading is gone. Passing `new_blocks=[]`
    deletes the section entirely.
    """
    o = _open_document(existing_encoded_collab)
    matches = _find_root_headings(o.blocks_map, o.text_map, o.root_children, heading)
    match, err = _resolve_match(matches, heading, match_index)
    if err is not None or match is None:
        return None, err
    start_index, _heading_id, level = match
    end_index = _section_end_index(o.blocks_map, o.root_children, start_index, level)
    with o.doc.transaction():
        _remove_root_range(o, start_index, end_index)
        _insert_root_blocks(o, new_blocks, start_index)
    return _finish(o, len(new_blocks)), None


def _insert_at_heading(
    existing_encoded_collab: bytes,
    heading: str,
    new_blocks: list[dict[str, Any]],
    match_index: int | None,
    before: bool,
) -> tuple[DocEdit | None, str | None]:
    """Shared implementation for insert_after_heading and insert_before_heading.

    `before=False` → insert at index+1 (right after the heading, top of section).
    `before=True`  → insert at index (right before the heading, end of previous
    section / new section above).
    """
    o = _open_document(existing_encoded_collab)
    matches = _find_root_headings(o.blocks_map, o.text_map, o.root_children, heading)
    match, err = _resolve_match(matches, heading, match_index)
    if err is not None or match is None:
        return None, err
    start_index, _heading_id, _level = match
    with o.doc.transaction():
        _insert_root_blocks(o, new_blocks, start_index if before else start_index + 1)
    return _finish(o, len(new_blocks)), None


def insert_after_heading_in_document(
    existing_encoded_collab: bytes,
    heading: str,
    new_blocks: list[dict[str, Any]],
    match_index: int | None = None,
) -> tuple[DocEdit | None, str | None]:
    """Insert new blocks immediately after a root-level heading (i.e. at the
    very top of that section). Returns (edit, error).

    Same matching/ambiguity rules as `replace_section_in_document`.
    """
    return _insert_at_heading(
        existing_encoded_collab, heading, new_blocks, match_index, before=False
    )


def insert_before_heading_in_document(
    existing_encoded_collab: bytes,
    heading: str,
    new_blocks: list[dict[str, Any]],
    match_index: int | None = None,
) -> tuple[DocEdit | None, str | None]:
    """Insert new blocks immediately before a root-level heading (i.e. at the
    very end of the previous section, or the beginning of the page if the
    heading is first). Returns (edit, error).

    Same matching/ambiguity rules as `replace_section_in_document`.
    """
    return _insert_at_heading(
        existing_encoded_collab, heading, new_blocks, match_index, before=True
    )


def append_blocks_to_document(
    existing_encoded_collab: bytes, blocks: list[dict[str, Any]]
) -> DocEdit:
    """Load an existing document and append `blocks` to the end of the root
    page. Existing content, inline formatting and CRDT clocks are untouched;
    the root children Y.Array is mutated in place (never reassigned, which
    would replace the array and lose its history)."""
    o = _open_document(existing_encoded_collab)
    with o.doc.transaction():
        _insert_root_blocks(o, blocks, len(o.root_children))
    return _finish(o, len(blocks))
