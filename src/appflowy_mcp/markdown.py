"""Render an AppFlowy document's decoded collab JSON to Markdown.

The decoded JSON from /api/workspace/v1/{ws}/collab/{view}/json (collab_type=0)
has shape:
    {
      "collab": {
        "document": {
          "page_id": "<root_block_id>",
          "blocks": { "<block_id>": {ty, parent, children, external_id, data, ...} },
          "meta": {
            "children_map": { "<children_key>": ["<child_block_id>", ...] },
            "text_map": { "<text_key>": "<plain_text_or_json_delta>" }
          }
        }
      }
    }

Blocks reference their child ordering via `children` (a children_map key) and
their text via `external_id` (a text_map key). Text is either a plain string
or a JSON-encoded Yjs delta (rich formatting); we handle both.
"""
import json
from typing import Any


def _delta_to_string(raw: str) -> str:
    """Yjs deltas serialize as `[{"insert": "...", "attributes": {...}}, ...]`.
    For plain text we just concatenate inserts. Formatting attrs are
    rendered as markdown marks where possible.
    """
    if not raw:
        return ""
    if not (raw.startswith("[") and raw.endswith("]")):
        return raw
    try:
        ops = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(ops, list):
        return raw

    out: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        ins = op.get("insert")
        if not isinstance(ins, str):
            continue
        attrs = op.get("attributes") or {}
        text = ins
        if attrs.get("code"):
            text = f"`{text}`"
        if attrs.get("bold"):
            text = f"**{text}**"
        if attrs.get("italic"):
            text = f"*{text}*"
        if attrs.get("strikethrough"):
            text = f"~~{text}~~"
        href = attrs.get("href")
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _block_text(block: dict[str, Any], text_map: dict[str, str]) -> str:
    key = block.get("external_id")
    if not key:
        return ""
    raw = text_map.get(key, "")
    if not isinstance(raw, str):
        return ""
    return _delta_to_string(raw)


def _block_data(block: dict[str, Any]) -> dict[str, Any]:
    raw = block.get("data")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _render_block(
    block_id: str,
    blocks: dict[str, Any],
    children_map: dict[str, list[str]],
    text_map: dict[str, str],
    depth: int,
    list_counters: dict[str, int],
) -> list[str]:
    block = blocks.get(block_id)
    if not block:
        return []

    text = _block_text(block, text_map)
    data = _block_data(block)
    ty = block.get("ty", "")
    indent = "  " * depth

    line: str | None
    if ty == "page":
        line = None  # root, no rendering
    elif ty == "heading":
        level = max(1, min(int(data.get("level", 1)), 6))
        line = f"{'#' * level} {text}"
    elif ty == "paragraph":
        line = text if depth == 0 else f"{indent}{text}"
    elif ty == "todo_list":
        mark = "x" if data.get("checked") else " "
        line = f"{indent}- [{mark}] {text}"
    elif ty == "bulleted_list":
        line = f"{indent}- {text}"
    elif ty == "numbered_list":
        parent_id = block.get("parent", "")
        list_counters[parent_id] = list_counters.get(parent_id, 0) + 1
        line = f"{indent}{list_counters[parent_id]}. {text}"
    elif ty == "quote":
        line = f"> {text}"
    elif ty == "callout":
        emoji = data.get("icon") or "💡"
        line = f"> {emoji} {text}"
    elif ty == "code":
        lang = data.get("language", "")
        line = f"```{lang}\n{text}\n```"
    elif ty == "divider":
        line = "---"
    elif ty == "toggle_list":
        line = f"{indent}- <details><summary>{text}</summary>"
    elif ty == "image":
        url = data.get("url", "")
        line = f"![{text or 'image'}]({url})"
    elif ty == "page" or ty == "child_page":
        line = f"{indent}- [{text or '(untitled)'}](#{block_id})"
    else:
        line = f"{indent}{text}" if text else None

    lines: list[str] = []
    if line is not None:
        lines.append(line)

    children_key = block.get("children")
    if children_key:
        for child_id in children_map.get(children_key, []):
            lines.extend(
                _render_block(
                    child_id, blocks, children_map, text_map, depth + 1, list_counters
                )
            )
    return lines


def render_document(collab_json: dict[str, Any]) -> str:
    """Convert decoded collab JSON to a markdown string."""
    doc = (collab_json or {}).get("collab", {}).get("document") or {}
    blocks = doc.get("blocks") or {}
    meta = doc.get("meta") or {}
    children_map = meta.get("children_map") or {}
    text_map = meta.get("text_map") or {}
    page_id = doc.get("page_id")

    if not page_id or page_id not in blocks:
        return ""

    list_counters: dict[str, int] = {}
    lines = _render_block(page_id, blocks, children_map, text_map, 0, list_counters)
    # Collapse runs of blank lines, add spacing between top-level blocks.
    out: list[str] = []
    for line in lines:
        if line.strip() == "" and out and out[-1] == "":
            continue
        out.append(line)
    return "\n\n".join(s for s in out if s != "").strip() + "\n"
