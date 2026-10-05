"""Pure-CRDT smoke test for Phase 2 partial-editing helpers.

Exercises replace_section_in_document and insert_after_heading_in_document
without touching an AppFlowy server. Builds a synthetic document, applies
edits, decodes, asserts on structure and rendered output.
"""
import json
import struct
import sys

from pycrdt import Doc, Map

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from appflowy_mcp.doc_builder import (
    append_blocks_to_document,
    build_document,
    insert_after_heading_in_document,
    insert_before_heading_in_document,
    replace_section_in_document,
)
from appflowy_mcp.markdown import render_document
from appflowy_mcp.markdown_to_blocks import parse


def encoded_v1_to_raw(encoded_v1: bytes) -> bytes:
    """Extract the inner raw doc_state from a bincode-wrapped EncodedCollab."""
    sv_len = struct.unpack_from("<Q", encoded_v1, 0)[0]
    ds_len = struct.unpack_from("<Q", encoded_v1, 8 + sv_len)[0]
    return encoded_v1[8 + sv_len + 8 : 8 + sv_len + 8 + ds_len]


def render(encoded_v1: bytes) -> str:
    raw = encoded_v1_to_raw(encoded_v1)
    doc = Doc()
    doc["data"] = Map({})
    doc.apply_update(raw)
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
    return render_document({"collab": {"document": {
        "page_id": document["page_id"],
        "blocks": blocks,
        "meta": {"children_map": children_map, "text_map": text_map},
    }}})


def root_block_ids(encoded_v1: bytes) -> list[str]:
    raw = encoded_v1_to_raw(encoded_v1)
    doc = Doc()
    doc["data"] = Map({})
    doc.apply_update(raw)
    document = doc["data"]["document"]
    page_id = document["page_id"]
    page_block = document["blocks"][page_id]
    return list(document["meta"]["children_map"][page_block["children"]])


def build_initial() -> bytes:
    md = """# Page Title

Intro paragraph.

## Alpha

Alpha body line 1.

Alpha body line 2.

## Beta

Beta body.

## Gamma

Gamma body."""
    return build_document(parse(md))


def test_replace_section() -> None:
    print("=== test_replace_section ===")
    enc = build_initial()
    before_ids = root_block_ids(enc)
    print(f"Initial root blocks: {len(before_ids)}")

    new_section_md = """## Beta

Beta replaced body, with **bold** and a [link](https://x.com)."""
    new_blocks = parse(new_section_md)
    raw = encoded_v1_to_raw(enc)

    new_enc, err = replace_section_in_document(raw, "Beta", new_blocks)
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert err is None, f"unexpected error: {err}"
    assert new_enc is not None

    after_ids = root_block_ids(new_enc)
    print(f"After replace: {len(after_ids)} root blocks")
    # Beta section was 2 blocks (heading + 1 paragraph); replacement is 2 blocks
    # (heading + 1 paragraph). Total must stay the same.
    assert len(after_ids) == len(before_ids), (
        f"expected {len(before_ids)} blocks after replace, got {len(after_ids)}"
    )

    rendered = render(new_enc)
    print(rendered)
    assert "Beta replaced body" in rendered
    assert "**bold**" in rendered
    assert "[link](https://x.com)" in rendered
    assert "Alpha" in rendered, "Alpha section must survive"
    assert "Gamma" in rendered, "Gamma section must survive"
    assert "Beta body." not in rendered, "old Beta body must be gone"
    print("OK\n")


def test_replace_section_delete() -> None:
    print("=== test_replace_section_delete (empty replacement) ===")
    enc = build_initial()
    raw = encoded_v1_to_raw(enc)

    new_enc, err = replace_section_in_document(raw, "Beta", [])
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert err is None
    rendered = render(new_enc)
    print(rendered)
    assert "## Beta" not in rendered
    assert "Beta body." not in rendered
    assert "## Alpha" in rendered
    assert "## Gamma" in rendered
    print("OK\n")


def test_replace_section_ambiguous() -> None:
    print("=== test_replace_section_ambiguous ===")
    md = """# Top

## Notes

First Notes section.

## Notes

Second Notes section."""
    enc = build_document(parse(md))
    raw = encoded_v1_to_raw(enc)

    new_enc, err = replace_section_in_document(raw, "Notes", parse("## New\n\nNew."))
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert new_enc is None
    assert err is not None and "multiple matches" in err
    print(f"Got expected error: {err}")

    # Now disambiguate with match_index=1 (second one)
    new_enc, err = replace_section_in_document(raw, "Notes", parse("## Renamed\n\nReplaced."), match_index=1)
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert err is None
    rendered = render(new_enc)
    print(rendered)
    assert "First Notes section." in rendered, "first Notes section must survive"
    assert "Second Notes section." not in rendered
    assert "## Renamed" in rendered
    print("OK\n")


def test_replace_section_not_found() -> None:
    print("=== test_replace_section_not_found ===")
    enc = build_initial()
    raw = encoded_v1_to_raw(enc)
    new_enc, err = replace_section_in_document(raw, "Delta", parse("## Delta\n\nbody"))
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert new_enc is None
    assert err is not None and "not found" in err
    print(f"Got expected error: {err}")
    print("OK\n")


def test_insert_after_heading() -> None:
    print("=== test_insert_after_heading ===")
    enc = build_initial()
    raw = encoded_v1_to_raw(enc)

    new_enc, err = insert_after_heading_in_document(raw, "Beta", parse("Inserted right after Beta heading."))
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert err is None
    rendered = render(new_enc)
    print(rendered)
    # Inserted block must appear between "## Beta" and "Beta body."
    beta_idx = rendered.index("## Beta")
    inserted_idx = rendered.index("Inserted right after")
    beta_body_idx = rendered.index("Beta body.")
    assert beta_idx < inserted_idx < beta_body_idx, (
        f"order broken: heading={beta_idx} inserted={inserted_idx} body={beta_body_idx}"
    )
    print("OK\n")


def test_insert_before_heading() -> None:
    print("=== test_insert_before_heading ===")
    enc = build_initial()
    raw = encoded_v1_to_raw(enc)

    new_enc, err = insert_before_heading_in_document(raw, "Beta", parse("## Inserted Before Beta\n\nNew section body."))
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert err is None
    rendered = render(new_enc)
    print(rendered)
    alpha_idx = rendered.index("## Alpha")
    inserted_idx = rendered.index("## Inserted Before Beta")
    beta_idx = rendered.index("## Beta")
    assert alpha_idx < inserted_idx < beta_idx, (
        f"order broken: alpha={alpha_idx} inserted={inserted_idx} beta={beta_idx}"
    )
    # Body of Alpha must still come before the inserted section
    assert rendered.index("Alpha body line 2.") < inserted_idx
    print("OK\n")


def test_insert_before_first_heading() -> None:
    print("=== test_insert_before_first_heading (edge: prepend) ===")
    enc = build_initial()
    raw = encoded_v1_to_raw(enc)

    new_enc, err = insert_before_heading_in_document(raw, "Alpha", parse("## Prepended\n\nVery first new section."))
    new_enc = new_enc.encoded_v1 if new_enc is not None else None
    assert err is None
    rendered = render(new_enc)
    print(rendered)
    prepended_idx = rendered.index("## Prepended")
    alpha_idx = rendered.index("## Alpha")
    intro_idx = rendered.index("Intro paragraph.")
    # Intro paragraph (first body after H1) must still come before the new prepended section
    assert intro_idx < prepended_idx < alpha_idx
    print("OK\n")


def test_replace_then_append() -> None:
    print("=== test_replace_then_append (combo) ===")
    enc = build_initial()
    raw = encoded_v1_to_raw(enc)

    enc2, err = replace_section_in_document(raw, "Alpha", parse("## Alpha\n\nAlpha new body."))
    enc2 = enc2.encoded_v1 if enc2 is not None else None
    assert err is None
    raw2 = encoded_v1_to_raw(enc2)

    enc3 = append_blocks_to_document(raw2, parse("## Delta\n\nFresh tail.")).encoded_v1
    rendered = render(enc3)
    print(rendered)
    assert "Alpha new body." in rendered
    assert "Beta body." in rendered
    assert "Gamma body." in rendered
    assert "## Delta" in rendered
    assert "Fresh tail." in rendered
    # Ordering: Delta should be at end
    assert rendered.index("## Alpha") < rendered.index("## Beta") < rendered.index("## Gamma") < rendered.index("## Delta")
    print("OK\n")


if __name__ == "__main__":
    test_replace_section()
    test_replace_section_delete()
    test_replace_section_ambiguous()
    test_replace_section_not_found()
    test_insert_after_heading()
    test_insert_before_heading()
    test_insert_before_first_heading()
    test_replace_then_append()
    print("All Phase 2 tests passed.")
