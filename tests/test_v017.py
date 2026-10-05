"""Unit tests for the v0.17.0 realtime write path.

Every document edit must come back as a DocEdit whose incremental `update`,
applied to a copy of the SERVER's state, yields the edited page, and whose
`encoded_v1` full state (the PUT fallback) decodes to the same page.
"""

from __future__ import annotations

import json
import struct
import unittest

from pycrdt import Doc, Map

from appflowy_mcp.doc_builder import (
    DocEdit,
    append_blocks_to_document,
    build_document,
    insert_after_heading_in_document,
    insert_before_heading_in_document,
    replace_content_in_document,
    replace_section_in_document,
)
from appflowy_mcp.markdown import render_document
from appflowy_mcp.markdown_to_blocks import parse


def _doc_state(encoded_v1: bytes) -> bytes:
    """Unwrap bincode EncodedCollab { state_vector, doc_state, version }."""
    (sv_len,) = struct.unpack_from("<Q", encoded_v1, 0)
    off = 8 + sv_len
    (ds_len,) = struct.unpack_from("<Q", encoded_v1, off)
    return encoded_v1[off + 8 : off + 8 + ds_len]


def _decode(doc: Doc) -> dict:
    """Same shape AppFlowyClient.get_document_decoded builds, from a live Doc."""
    document = doc.get("data", type=Map)["document"]
    blocks = {bid: {k: b[k] for k in b.keys()} for bid, b in ((bid, document["blocks"][bid]) for bid in document["blocks"].keys())}
    cm = document["meta"]["children_map"]
    children = {ck: list(cm[ck]) for ck in cm.keys()}
    tm = document["meta"]["text_map"]
    texts = {}
    for tk in tm.keys():
        runs = tm[tk].diff()
        if not runs:
            texts[tk] = ""
        elif len(runs) == 1 and not runs[0][1]:
            texts[tk] = runs[0][0]
        else:
            texts[tk] = json.dumps([{"insert": c, **({"attributes": a} if a else {})} for c, a in runs])
    return {"collab": {"document": {"page_id": document["page_id"], "blocks": blocks,
                                     "meta": {"children_map": children, "text_map": texts}}}}


def _render(raw: bytes, *updates: bytes) -> str:
    doc = Doc()
    doc.apply_update(raw)
    for u in updates:
        doc.apply_update(u)
    return render_document(_decode(doc))


BASE_MD = "# Title\n\nIntro paragraph.\n\n## Alpha\n\n- a1\n- a2\n\n## Omega\n\nLast words."


class TestRealtimeEdits(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _doc_state(build_document(parse(BASE_MD)))
        self.base_md = _render(self.raw)
        self.assertIn("Intro paragraph.", self.base_md)

    def _check(self, edit: DocEdit, must_have: list[str], must_not: list[str] = ()) -> str:
        self.assertIsInstance(edit, DocEdit)
        # The incremental update applied on the server's copy gives the edit.
        via_update = _render(self.raw, edit.update)
        # The PUT fallback decodes to the same page.
        via_full = _render(_doc_state(edit.encoded_v1))
        self.assertEqual(via_update, via_full)
        for text in must_have:
            self.assertIn(text, via_update)
        for text in must_not:
            self.assertNotIn(text, via_update)
        # The delta is what travels: smaller than the whole document.
        self.assertLess(len(edit.update), len(edit.encoded_v1))
        return via_update

    def test_append(self):
        edit = append_blocks_to_document(self.raw, parse("- appended **bold**"))
        md = self._check(edit, ["Intro paragraph.", "Last words.", "appended **bold**"])
        self.assertEqual(edit.blocks_written, 1)
        self.assertGreater(md.index("appended"), md.index("Last words."))

    def test_replace_content_keeps_page_and_drops_old_blocks(self):
        edit = replace_content_in_document(self.raw, parse("# New\n\nOnly this."))
        self._check(edit, ["Only this."], ["Intro paragraph.", "Alpha", "Last words."])
        doc = Doc()
        doc.apply_update(self.raw)
        doc.apply_update(edit.update)
        document = doc.get("data", type=Map)["document"]
        # page block + heading + paragraph, nothing stale left behind
        self.assertEqual(len(list(document["blocks"].keys())), 3)
        self.assertEqual(len(list(document["meta"]["text_map"].keys())), 2)

    def test_replace_section(self):
        edit, err = replace_section_in_document(self.raw, "alpha", parse("## Alpha\n\nrewritten"))
        self.assertIsNone(err)
        md = self._check(edit, ["rewritten", "Last words.", "Intro paragraph."], ["a1", "a2"])
        self.assertLess(md.index("rewritten"), md.index("Omega"))

    def test_replace_section_delete_and_errors(self):
        edit, err = replace_section_in_document(self.raw, "Omega", [])
        self.assertIsNone(err)
        self._check(edit, ["a2"], ["Omega", "Last words."])
        edit, err = replace_section_in_document(self.raw, "Nope", parse("x"))
        self.assertIsNone(edit)
        self.assertIn("not found", err)

    def test_insert_after_and_before(self):
        edit, err = insert_after_heading_in_document(self.raw, "Alpha", parse("first in section"))
        self.assertIsNone(err)
        md = self._check(edit, ["first in section"])
        self.assertLess(md.index("first in section"), md.index("a1"))
        edit, err = insert_before_heading_in_document(self.raw, "Omega", parse("## Mid\n\nbetween"))
        self.assertIsNone(err)
        md = self._check(edit, ["between"])
        self.assertLess(md.index("a2"), md.index("between"))
        self.assertLess(md.index("between"), md.index("Omega"))

    def test_update_is_a_pure_delta(self):
        edit = append_blocks_to_document(self.raw, parse("tail"))
        # Applying the delta to an EMPTY doc must not yield a readable page:
        # it depends on the server state, which is the point of a delta.
        empty = Doc()
        empty.apply_update(edit.update)
        root = empty.get("data", type=Map)
        self.assertNotIn("page_id", list(root["document"].keys()) if "document" in root else [])
