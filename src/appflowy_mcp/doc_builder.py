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

Output bytes are bincode-serialized `EncodedCollab { state_vector, doc_state, version=V1 }`
ready to be sent as `encoded_collab_v1` to `PUT /api/workspace/{ws}/collab/{object_id}`.
"""
import json
import struct
import uuid
from typing import Any

from pycrdt import Array, Doc, Map, Text


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
    text_key = _new_key("txt") if block.get("text") is not None else ""

    blocks_map[block_id] = Map({
        "id": block_id,
        "ty": block["ty"],
        "parent": parent_id,
        "children": children_key,
        "data": json.dumps(block.get("data") or {}),
        "external_id": text_key,
        "external_type": "text" if text_key else "",
    })

    # Children ordering: build first, then insert into children_map
    child_ids: list[str] = []
    for child in block.get("children") or []:
        cid = _add_block(blocks_map, children_map, text_map, block_id, child)
        child_ids.append(cid)
    children_map[children_key] = Array(child_ids)

    if text_key:
        text_map[text_key] = Text(block.get("text") or "")

    return block_id


def build_document(blocks: list[dict[str, Any]]) -> bytes:
    """Build a complete AppFlowy document Y.Doc and serialize to bincode bytes."""
    doc = Doc()
    data_map = Map({})
    doc["data"] = data_map

    document = Map({})
    data_map["document"] = document

    page_id = _new_key("page")
    page_children_key = _new_key("ch")
    document["page_id"] = page_id

    blocks_map = Map({})
    document["blocks"] = blocks_map
    blocks_map[page_id] = Map({
        "id": page_id,
        "ty": "page",
        "parent": "",
        "children": page_children_key,
        "data": "{}",
        "external_id": "",
        "external_type": "",
    })

    meta = Map({})
    document["meta"] = meta
    children_map = Map({})
    meta["children_map"] = children_map
    text_map = Map({})
    meta["text_map"] = text_map

    top_ids: list[str] = []
    for b in blocks:
        bid = _add_block(blocks_map, children_map, text_map, page_id, b)
        top_ids.append(bid)
    children_map[page_children_key] = Array(top_ids)

    doc_state = doc.get_update()
    state_vector = doc.get_state()
    return _encode_encoded_collab(state_vector, doc_state, version=0)
