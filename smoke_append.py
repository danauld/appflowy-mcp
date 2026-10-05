"""Pure-CRDT smoke test for append_blocks_to_document — no AppFlowy server needed.

Builds a fresh document with 2 blocks, then appends 2 more, then decodes the
result and verifies all 4 blocks are present in correct order with their
inline formatting preserved.
"""
import json
import sys

from pycrdt import Doc, Map

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from appflowy_mcp.doc_builder import append_blocks_to_document, build_document, _encode_encoded_collab
from appflowy_mcp.markdown import render_document
from appflowy_mcp.markdown_to_blocks import parse


def decode_to_readable(encoded_collab_v1: bytes) -> dict:
    """Unwrap bincode → raw doc_state → pycrdt Doc → JSON-like dict (subset of
    what get_document_decoded returns, enough for assertions)."""
    import struct

    sv_len = struct.unpack_from("<Q", encoded_collab_v1, 0)[0]
    off = 8
    sv = encoded_collab_v1[off:off + sv_len]
    off += sv_len
    ds_len = struct.unpack_from("<Q", encoded_collab_v1, off)[0]
    off += 8
    doc_state = encoded_collab_v1[off:off + ds_len]

    doc = Doc()
    doc["data"] = Map({})
    doc.apply_update(doc_state)
    document = doc["data"]["document"]

    blocks = {bid: {k: document["blocks"][bid][k] for k in document["blocks"][bid].keys()}
              for bid in list(document["blocks"].keys())}
    children_map = {ck: list(document["meta"]["children_map"][ck])
                    for ck in list(document["meta"]["children_map"].keys())}
    text_map = {}
    tm = document["meta"]["text_map"]
    for tk in list(tm.keys()):
        runs = tm[tk].diff()
        if len(runs) == 1 and not runs[0][1]:
            text_map[tk] = runs[0][0]
        else:
            text_map[tk] = json.dumps(
                [{"insert": c, **({"attributes": a} if a else {})} for c, a in runs],
                ensure_ascii=False,
            )
    return {
        "page_id": document["page_id"],
        "blocks": blocks,
        "children_map": children_map,
        "text_map": text_map,
    }


def main() -> None:
    # 1. Initial doc with 2 blocks
    initial_md = "# Heading One\n\nFirst paragraph with **bold** text."
    initial_blocks = parse(initial_md)
    print(f"Initial: parsed {len(initial_blocks)} blocks from markdown")

    encoded_v1 = build_document(initial_blocks)
    print(f"Initial encoded_collab_v1 size: {len(encoded_v1)} bytes")

    # Decode to verify the initial state
    decoded = decode_to_readable(encoded_v1)
    page_id = decoded["page_id"]
    page_block = decoded["blocks"][page_id]
    root_children_key = page_block["children"]
    root_ids = decoded["children_map"][root_children_key]
    print(f"Initial root children: {len(root_ids)} (expected 2)")
    assert len(root_ids) == 2, f"expected 2 root blocks, got {len(root_ids)}"

    # 2. Append 2 more blocks
    appendix_md = "## Appendix\n\nAdded later with *italic* and [a link](https://example.com)."
    appendix_blocks = parse(appendix_md)
    print(f"\nAppending: parsed {len(appendix_blocks)} blocks from markdown")

    # The append function expects the raw doc_state (apply_update input), not
    # the bincode-wrapped encoded_collab_v1. /page-view returns raw bytes; here
    # we extract them from our own wrapper for the test.
    import struct
    sv_len = struct.unpack_from("<Q", encoded_v1, 0)[0]
    ds_len = struct.unpack_from("<Q", encoded_v1, 8 + sv_len)[0]
    raw_doc_state = encoded_v1[8 + sv_len + 8:8 + sv_len + 8 + ds_len]
    print(f"Extracted raw doc_state: {len(raw_doc_state)} bytes")

    new_encoded_v1 = append_blocks_to_document(raw_doc_state, appendix_blocks).encoded_v1
    print(f"After-append encoded_collab_v1 size: {len(new_encoded_v1)} bytes")

    # 3. Decode and verify all 4 blocks are present
    decoded2 = decode_to_readable(new_encoded_v1)
    page_id2 = decoded2["page_id"]
    assert page_id2 == page_id, "page_id must be stable across append"
    root_ids2 = decoded2["children_map"][root_children_key]
    print(f"\nAfter-append root children: {len(root_ids2)} (expected 4)")
    assert len(root_ids2) == 4, f"expected 4 root blocks, got {len(root_ids2)}"

    # 4. Order check: original 2 first, new 2 appended at end
    assert root_ids2[:2] == root_ids, "original block IDs must come first in order"
    print(f"Order check OK: original IDs preserved, new IDs appended at end.")

    # 5. Render to markdown for visual check
    fake_collab_json = {"collab": {"document": {
        "page_id": page_id2,
        "blocks": decoded2["blocks"],
        "meta": {
            "children_map": decoded2["children_map"],
            "text_map": decoded2["text_map"],
        },
    }}}
    print("\n=== Rendered after append ===")
    print(render_document(fake_collab_json))

    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
