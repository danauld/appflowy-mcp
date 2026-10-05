#!/usr/bin/env python3
"""Rewrite Relation cells from the pre-0.16 JSON-string shape to AppFlowy's array.

appflowy-mcp <= 0.15.1 stored a Relation cell as the string '[{"id": "..."}]'.
AppFlowy reads a Relation cell only when its `data` is an array of row-id
strings, so those links never showed in the UI although the MCP read them back
fine. This script finds such cells and rewrites them through the same realtime
`web-update` path `update_database_row` uses, so open AppFlowy clients pick the
change up live.

Run it inside the appflowy-mcp container (it needs the in-stack base URL and
the bot credentials from the environment; nothing is printed but ids and
counts):

    docker exec -i appflowy-mcp python3 - --workspace <ws> < scripts/repair_relation_cells.py
    docker exec -i appflowy-mcp python3 - --workspace <ws> --apply < scripts/repair_relation_cells.py

Without --apply it only reports. --database <id> limits it to one database
(repeatable); the default is every database in the workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from appflowy_mcp.client import AppFlowyClient, AppFlowyError
from appflowy_mcp.database_collab import (
    extract_collab_cells,
    get_field_type_int,
    parse_relation_row_ids,
    relation_cell_is_legacy,
)


async def repair(ws: str, databases: list[str], apply: bool) -> int:
    client = AppFlowyClient(
        os.environ["APPFLOWY_BASE_URL"],
        os.environ["APPFLOWY_BOT_EMAIL"],
        os.environ["APPFLOWY_BOT_PASSWORD"],
        verify=os.environ.get("APPFLOWY_TLS_VERIFY", "true").lower() in ("true", "1", "yes"),
    )
    legacy = fixed = failed = 0
    try:
        if not databases:
            databases = [d["id"] for d in await client.list_databases(ws) if d.get("id")]
        for db in databases:
            fields = await client.get_database_fields(ws, db)
            # The REST field list names the type ("Relation"), the collab uses 10.
            rel = {str(f["id"]): str(f.get("name")) for f in fields if get_field_type_int(f) == 10}
            if not rel:
                continue
            ids = await client.get_database_row_ids(ws, db)
            print(f"database {db}: {len(ids)} rows, relation fields {sorted(rel.values())}")
            for rid in ids:
                try:
                    cells = extract_collab_cells(await client.get_collab_json(ws, rid, 4))
                except AppFlowyError as exc:
                    failed += 1
                    print(f"  row {rid}: read failed: {str(exc)[:120]}")
                    continue
                todo: dict[str, list[str]] = {}
                for fid, fname in rel.items():
                    cell = cells.get(fid)
                    data = cell.get("data") if isinstance(cell, dict) else None
                    if relation_cell_is_legacy(data):
                        todo[fname] = parse_relation_row_ids(data)
                if not todo:
                    continue
                legacy += len(todo)
                summary = ", ".join(f"{k}={len(v)} link(s)" for k, v in todo.items())
                if not apply:
                    print(f"  row {rid}: legacy {summary}")
                    continue
                try:
                    await client.update_database_row(ws, db, rid, todo)
                    fixed += len(todo)
                    print(f"  row {rid}: rewrote {summary}")
                except Exception as exc:  # noqa: BLE001 - report and continue
                    failed += 1
                    print(f"  row {rid}: write failed: {str(exc)[:120]}")
    finally:
        await client.aclose()
    print(f"\nlegacy cells: {legacy}  rewritten: {fixed}  failures: {failed}  mode: {'apply' if apply else 'dry-run'}")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", required=True, help="workspace UUID")
    ap.add_argument("--database", action="append", default=[], help="database id (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = ap.parse_args()
    sys.exit(asyncio.run(repair(args.workspace, args.database, args.apply)))


if __name__ == "__main__":
    main()
