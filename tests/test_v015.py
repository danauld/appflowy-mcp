"""Unit tests for appflowy-mcp v0.15.0 features.

Tests row body document ID derivation, relation parsing, in-place row editing,
select option creation, and database tools.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pycrdt import Doc, Map

from appflowy_mcp.database_collab import (
    FIELD_TYPE_NAMES,
    SELECT_COLORS,
    apply_add_select_option,
    apply_row_cells_update,
    decode_collab_doc,
    extract_collab_cells,
    get_field_select_options,
    get_field_type_int,
    parse_relation_row_ids,
    resolve_cells_dict,
    row_to_document_id,
)
from appflowy_mcp.client import AppFlowyClient
from appflowy_mcp.config import Config
from appflowy_mcp.server import build_server


class TestDatabaseCollab(unittest.TestCase):
    def test_row_to_document_id(self):
        row_id = "94c65ee3-80a6-4eda-bf54-08f5004c96f9"
        doc_id = row_to_document_id(row_id)
        self.assertEqual(doc_id, "ceef6367-8e35-566f-b615-919bfd5be514")

    def test_parse_relation_row_ids(self):
        # Empty
        self.assertEqual(parse_relation_row_ids(None), [])
        self.assertEqual(parse_relation_row_ids(""), [])
        self.assertEqual(parse_relation_row_ids([]), [])

        # Single UUID string
        u1 = "94c65ee3-80a6-4eda-bf54-08f5004c96f9"
        self.assertEqual(parse_relation_row_ids(u1), [u1])

        # Comma-separated
        u2 = "5216e20a-7e60-4f05-90a5-26001f6a4c77"
        self.assertEqual(parse_relation_row_ids(f"{u1}, {u2}"), [u1, u2])

        # JSON list of strings
        self.assertEqual(parse_relation_row_ids(json.dumps([u1, u2])), [u1, u2])

        # JSON list of dicts with "id"
        self.assertEqual(
            parse_relation_row_ids(json.dumps([{"id": u1}, {"id": u2}])),
            [u1, u2],
        )

        # JSON object with "rows"
        self.assertEqual(
            parse_relation_row_ids(json.dumps({"rows": [{"id": u1}]})),
            [u1],
        )

    def test_extract_collab_cells(self):
        # Nested format
        data1 = {
            "collab": {
                "data": {
                    "cells": {
                        "f1": {"data": "val1"}
                    }
                }
            }
        }
        self.assertEqual(extract_collab_cells(data1), {"f1": {"data": "val1"}})

        # Double nested data.data.cells
        data2 = {
            "collab": {
                "data": {
                    "data": {
                        "cells": {
                            "f2": {"data": "val2"}
                        }
                    }
                }
            }
        }
        self.assertEqual(extract_collab_cells(data2), {"f2": {"data": "val2"}})

    def test_resolve_cells_dict(self):
        fields = [
            {"id": "f_text", "name": "Title", "field_type": 0},
            {"id": "f_num", "name": "Count", "field_type": 1},
            {"id": "f_date", "name": "Due", "field_type": 2},
            {
                "id": "f_select",
                "name": "Status",
                "field_type": 3,
                "type_option": {
                    "content": json.dumps({"options": [{"id": "opt_todo", "name": "To Do"}]})
                },
            },
            {"id": "f_check", "name": "Done", "field_type": 5},
            {"id": "f_rel", "name": "Tasks", "field_type": 10},
        ]

        cells = {
            "Title": "My Task",
            "Count": 42,
            "Due": "2026-09-25T00:00:00Z",
            "Status": "To Do",
            "Done": True,
            "Tasks": ["94c65ee3-80a6-4eda-bf54-08f5004c96f9"],
        }

        resolved = resolve_cells_dict(fields, cells)
        self.assertEqual(resolved["f_text"], (0, "My Task"))
        self.assertEqual(resolved["f_num"], (1, "42"))
        self.assertTrue(resolved["f_date"][1].isdigit())
        self.assertEqual(resolved["f_select"], (3, "opt_todo"))
        self.assertEqual(resolved["f_check"], (5, "true"))
        self.assertIn("94c65ee3-80a6-4eda-bf54-08f5004c96f9", resolved["f_rel"][1])

    def test_apply_row_cells_update(self):
        doc = Doc()
        resolved = {
            "f1": (0, "Initial Title"),
            "f2": (5, "true"),
        }
        update = apply_row_cells_update(doc, resolved)
        self.assertTrue(len(update) > 0)

        # Apply update to another Doc and verify
        doc2 = Doc()
        doc2["data"] = Map({})
        doc2.apply_update(doc.get_update())
        cells = doc2["data"]["data"]["cells"]
        self.assertEqual(cells["f1"]["data"], "Initial Title")
        self.assertEqual(cells["f2"]["data"], "true")

    def test_apply_add_select_option(self):
        doc = Doc()
        doc["data"] = Map({
            "database": Map({
                "fields": Map({
                    "fld1": Map({
                        "name": "Status",
                        "ty": 3,
                        "type_option": Map({
                            "3": Map({"content": json.dumps({"options": []})})
                        })
                    })
                })
            })
        })

        opt_id, is_ext, update = apply_add_select_option(
            doc, "fld1", 3, "In Progress", "Yellow"
        )
        self.assertFalse(is_ext)
        self.assertTrue(len(opt_id) > 0)
        self.assertTrue(len(update) > 0)

        # Adding same option name again is idempotent
        opt_id2, is_ext2, update2 = apply_add_select_option(
            doc, "fld1", 3, "In Progress", "Yellow"
        )
        self.assertTrue(is_ext2)
        self.assertEqual(opt_id, opt_id2)
        self.assertEqual(len(update2), 0)


class TestAppFlowyServerTools(unittest.IsolatedAsyncioTestCase):
    async def test_tool_registration_count(self):
        cfg = Config(
            base_url="http://localhost",
            host="0.0.0.0",
            port=8765,
            transport="http",
            tls_verify=True,
        )
        mcp, pool = build_server(cfg)
        tools = mcp._tool_manager._tools
        self.assertIn("update_database_row", tools)
        self.assertIn("add_select_option", tools)
        self.assertIn("get_database_rows", tools)
        self.assertIn("read_page", tools)
        self.assertIn("get_database_fields", tools)


if __name__ == "__main__":
    unittest.main()
