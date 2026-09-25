"""AppFlowy database & row collab (CRDT) helpers.

Handles in-place row cell updates, Relation cell resolution, select options,
and row body document ID derivation.
"""

from __future__ import annotations

import base64
from datetime import datetime
import json
import secrets
import time
from typing import Any
import uuid

from pycrdt import Array, Doc, Map

SELECT_COLORS = [
    "Purple",
    "Pink",
    "LightPink",
    "Orange",
    "Yellow",
    "Lime",
    "Green",
    "Aqua",
    "Blue",
    "Cream",
    "Mint",
    "Sky",
    "Lilac",
    "Pearl",
    "Sunset",
    "Coral",
    "Sapphire",
    "Moss",
    "Sand",
    "Charcoal",
]

FIELD_TYPE_NAMES: dict[int, str] = {
    0: "RichText",
    1: "Number",
    2: "DateTime",
    3: "SingleSelect",
    4: "MultiSelect",
    5: "Checkbox",
    6: "URL",
    7: "Checklist",
    8: "LastEditedTime",
    9: "CreatedTime",
    10: "Relation",
    11: "Summary",
    12: "Translate",
    13: "Time",
    14: "Media",
}

FIELD_TYPE_NAME_TO_INT: dict[str, int] = {
    v.lower(): k for k, v in FIELD_TYPE_NAMES.items()
}


def row_to_document_id(row_id: str) -> str:
    """A database row's body document (the card page below the row) is a separate
    collab; AppFlowy derives its object id as uuid5(row_uuid, "document_id")."""
    return str(uuid.uuid5(uuid.UUID(str(row_id)), "document_id"))


def parse_relation_row_ids(data: Any) -> list[str]:
    """Parse linked row UUIDs from a Relation cell's raw value.

    AppFlowy can store relation cell data in several formats:
    - JSON list of strings: `["uuid-1", "uuid-2"]`
    - JSON list of objects: `[{"id": "uuid-1"}, {"id": "uuid-2"}]`
    - JSON object: `{"rows": [...]}`
    - Comma or space separated string: `"uuid-1,uuid-2"`
    """
    if not data:
        return []
    if isinstance(data, list):
        out: list[str] = []
        for item in data:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict) and "id" in item:
                out.append(str(item["id"]).strip())
        return out
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        if (text.startswith("[") and text.endswith("]")) or (
            text.startswith("{") and text.endswith("}")
        ):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parse_relation_row_ids(parsed)
                if isinstance(parsed, dict):
                    if "rows" in parsed and isinstance(parsed["rows"], list):
                        return parse_relation_row_ids(parsed["rows"])
                    if "id" in parsed:
                        return [str(parsed["id"]).strip()]
            except Exception:
                pass
        # Fallback: comma or space separated tokens
        tokens = [t.strip() for t in text.replace(",", " ").split() if t.strip()]
        valid_uuids: list[str] = []
        for token in tokens:
            try:
                uuid.UUID(token)
                valid_uuids.append(token)
            except ValueError:
                # If not a strict UUID, include anyway if token looks plausible
                if len(token) >= 8:
                    valid_uuids.append(token)
        return valid_uuids
    return []


def extract_collab_cells(collab_data: dict[str, Any]) -> dict[str, Any]:
    """Extract cells map from /collab/{id}/json endpoint output for DatabaseRow."""
    if not isinstance(collab_data, dict):
        return {}
    target = collab_data.get("collab") or collab_data
    if isinstance(target, dict):
        # Could be target.data.data.cells or target.data.cells
        if "data" in target and isinstance(target["data"], dict):
            sub = target["data"]
            if "data" in sub and isinstance(sub["data"], dict):
                inner = sub["data"]
                if "cells" in inner and isinstance(inner["cells"], dict):
                    return inner["cells"]
            if "cells" in sub and isinstance(sub["cells"], dict):
                return sub["cells"]
        if "cells" in target and isinstance(target["cells"], dict):
            return target["cells"]
    return {}


def decode_collab_doc(payload: dict[str, Any]) -> Doc:
    """Decode an AppFlowy collab API response into a pycrdt Doc."""
    node: Any = payload
    if isinstance(node, dict):
        for key in ("data", "encode_collab", "encoded_collab", "encode_collab_v1", "collab"):
            if isinstance(node.get(key), dict):
                node = node[key]
    ds = node.get("doc_state") if isinstance(node, dict) else None
    if ds is None and isinstance(payload, dict):
        ds = payload.get("doc_state")

    doc = Doc()
    if ds is not None:
        raw_bytes = base64.b64decode(ds) if isinstance(ds, str) else bytes(ds)
        if raw_bytes:
            doc.apply_update(raw_bytes)
    return doc


def get_field_type_int(field: dict[str, Any]) -> int:
    """Get the integer field type from a field dictionary."""
    ft = field.get("field_type")
    if ft is None:
        ft = field.get("field_type_id") or field.get("ty")
    if isinstance(ft, int):
        return ft
    if isinstance(ft, str):
        ft_lower = ft.strip().lower()
        if ft_lower in FIELD_TYPE_NAME_TO_INT:
            return FIELD_TYPE_NAME_TO_INT[ft_lower]
        if ft.isdigit():
            return int(ft)
    return 0


def get_field_select_options(field: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract list of {id, name, color} options for select fields."""
    to = field.get("type_option")
    if not to:
        return []
    content: Any = None
    if isinstance(to, dict):
        content = to.get("content")
        if content is None:
            # Check keyed by field type "3" or "4"
            for k in ("3", "4"):
                if k in to and isinstance(to[k], dict):
                    content = to[k].get("content")
                    break
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            content = None
    if isinstance(content, dict):
        opts = content.get("options")
        if isinstance(opts, list):
            return opts
    return []


def _resolve_select_option_id(field: dict[str, Any], val: Any) -> str:
    """Resolve an option name or id to an option id."""
    options = get_field_select_options(field)
    wanted = str(val).strip()
    if not wanted:
        return ""
    for opt in options:
        if opt.get("id") == wanted or opt.get("name") == wanted:
            return str(opt["id"])
    avail = [repr(o.get("name")) for o in options if o.get("name")]
    avail_str = ", ".join(avail) if avail else "none defined"
    raise ValueError(
        f"field {field.get('name')!r} has no option {wanted!r}; available options: {avail_str}"
    )


def encode_cell_value(field: dict[str, Any], val: Any) -> tuple[int, str]:
    """Encode a Python value into (field_type_int, stored_string_value)."""
    ft_int = get_field_type_int(field)
    fname = field.get("name", "unknown")

    # 3: SingleSelect
    if ft_int == 3:
        if val is None or val == "":
            return ft_int, ""
        return ft_int, _resolve_select_option_id(field, val)

    # 4: MultiSelect
    if ft_int == 4:
        if val is None or val == "" or val == []:
            return ft_int, ""
        items = val if isinstance(val, list) else [p.strip() for p in str(val).split(",") if p.strip()]
        resolved_ids = [_resolve_select_option_id(field, item) for item in items]
        return ft_int, ",".join(resolved_ids)

    # 5: Checkbox
    if ft_int == 5:
        if isinstance(val, str):
            res = "true" if val.strip().lower() in ("true", "yes", "1") else "false"
        else:
            res = "true" if bool(val) else "false"
        return ft_int, res

    # 1: Number
    if ft_int == 1:
        if isinstance(val, bool):
            raise ValueError(f"field {fname!r} is a Number field; got boolean {val!r}")
        return ft_int, str(val)

    # 2: DateTime
    if ft_int == 2:
        if val is None or val == "":
            return ft_int, ""
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return ft_int, str(int(val))
        text = str(val).strip()
        if text.isdigit():
            return ft_int, text
        try:
            ts = int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
            return ft_int, str(ts)
        except ValueError as exc:
            raise ValueError(
                f"field {fname!r} value {text!r} is not a valid timestamp or ISO-8601 date"
            ) from exc

    # 10: Relation
    if ft_int == 10:
        if val is None or val == "" or val == []:
            return ft_int, "[]"
        rids = parse_relation_row_ids(val)
        return ft_int, json.dumps([{"id": rid} for rid in rids])

    # 0: RichText, 6: URL, etc.
    return ft_int, "" if val is None else str(val)


def resolve_cells_dict(
    fields: list[dict[str, Any]], cells: dict[str, Any]
) -> dict[str, tuple[int, str]]:
    """Map user {field_name_or_id: value} -> {field_id: (field_type_int, encoded_string)}."""
    name_map: dict[str, dict[str, Any]] = {}
    id_map: dict[str, dict[str, Any]] = {}
    for f in fields:
        if f.get("id"):
            id_map[str(f["id"])] = f
        if f.get("name"):
            name_map[str(f["name"])] = f

    resolved: dict[str, tuple[int, str]] = {}
    for key, val in cells.items():
        field = name_map.get(str(key)) or id_map.get(str(key))
        if field is None:
            avail = ", ".join(repr(k) for k in name_map.keys())
            raise ValueError(f"unknown field {key!r}; available fields: {avail}")
        field_id = str(field["id"])
        resolved[field_id] = encode_cell_value(field, val)
    return resolved


def apply_row_cells_update(
    doc: Doc, resolved_cells: dict[str, tuple[int, str]]
) -> bytes:
    """Mutate row collab cells in a transaction and return the Yjs incremental update."""
    sv = doc.get_state()
    now = int(time.time())
    with doc.transaction():
        # Ensure root "data" exists
        root = doc.get("data", type=Map)
        if "data" not in root or root["data"] is None:
            root["data"] = Map({})
        data_map = root["data"]
        if "cells" not in data_map or data_map["cells"] is None:
            data_map["cells"] = Map({})
        cells_map = data_map["cells"]

        for fid, (fty, enc_val) in resolved_cells.items():
            if fid in cells_map:
                cell = cells_map[fid]
                cell["data"] = enc_val
                cell["last_modified"] = now
            else:
                cells_map[fid] = Map({
                    "field_type": fty,
                    "data": enc_val,
                    "created_at": now,
                    "last_modified": now,
                })
        data_map["last_modified"] = now

    return doc.get_update(sv)


def apply_add_select_option(
    doc: Doc,
    field_id: str,
    field_type_int: int,
    name: str,
    color: str = "Purple",
) -> tuple[str, bool, bytes]:
    """Add a select option to a database collab (type 1).

    Returns (option_id, is_existing, update_bytes).
    """
    if color not in SELECT_COLORS:
        avail = ", ".join(SELECT_COLORS)
        raise ValueError(f"color {color!r} is invalid; choose from: {avail}")

    root = doc.get("data", type=Map)
    if "database" not in root:
        raise ValueError("database collab is missing 'database' map")
    database_map = root["database"]
    if "fields" not in database_map:
        raise ValueError("database collab is missing 'fields' map")
    fields_map = database_map["fields"]
    if field_id not in fields_map:
        raise ValueError(f"field {field_id!r} not found in database collab")

    field_map = fields_map[field_id]
    tk = str(field_type_int)

    if "type_option" not in field_map:
        field_map["type_option"] = Map({})
    to_map = field_map["type_option"]
    if tk not in to_map:
        to_map[tk] = Map({})

    content_str = to_map[tk].get("content") or ""
    try:
        content_dict = json.loads(content_str) if content_str else {}
    except Exception:
        content_dict = {}

    options_list = content_dict.setdefault("options", [])
    content_dict.setdefault("disable_color", False)

    for opt in options_list:
        if opt.get("name") == name:
            # Already exists
            return opt["id"], True, b""

    # Generate short option id
    existing_ids = {o.get("id") for o in options_list}
    new_id = secrets.token_hex(2)
    while new_id in existing_ids:
        new_id = secrets.token_hex(2)

    sv = doc.get_state()
    with doc.transaction():
        options_list.append({"id": new_id, "name": name, "color": color})
        to_map[tk]["content"] = json.dumps(content_dict, separators=(",", ":"))
        field_map["last_modified"] = int(time.time())

    return new_id, False, doc.get_update(sv)
