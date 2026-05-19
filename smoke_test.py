"""Bypass MCP layer; exercise the AppFlowy HTTP client directly."""
import asyncio
import json
import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from appflowy_mcp.client import AppFlowyClient
from appflowy_mcp.config import Config


def dump(label: str, obj) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str)[:2000])


async def main() -> None:
    load_dotenv()
    cfg = Config.from_env()
    print(f"Connecting to {cfg.base_url} as {cfg.email} (verify={cfg.tls_verify})")
    client = AppFlowyClient(
        base_url=cfg.base_url,
        email=cfg.email,
        password=cfg.password,
        verify=cfg.tls_verify,
    )
    try:
        ws = await client.list_workspaces()
        dump(f"list_workspaces ({len(ws)})", ws)
        if not ws:
            print("No workspaces — stopping.")
            return
        ws_id = ws[0]["workspace_id"]

        folder = await client.get_folder(ws_id, depth=10)
        dump("get_folder (raw)", folder)

        def first_document(node, root_ws_id):
            for ch in node.get("children") or []:
                found = first_document(ch, root_ws_id)
                if found:
                    return found
            if (
                node.get("layout") == 0
                and not node.get("is_space")
                and node.get("view_id") != root_ws_id
            ):
                return node
            return None

        doc = first_document(folder, ws_id)
        if not doc:
            print("\nNo Document-layout view found.")
            return
        print(f"\nPicked document: {doc['name']} ({doc['view_id']})")

        page = await client.get_page_view(ws_id, doc["view_id"])
        dump("get_page_view (trimmed)", {
            "view": page.get("view"),
            "owner": page.get("owner"),
            "last_editor": page.get("last_editor"),
            "data_keys": list((page.get("data") or {}).keys()),
        })

        body = await client.get_collab_json(ws_id, doc["view_id"], collab_type=0)
        with open("collab_dump.json", "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
        print(f"\nFull collab JSON written to collab_dump.json")
        # Show top-level keys + look for any non-block sections
        top = body.get("collab") or {}
        doc_section = top.get("document") or {}
        print(f"  top-level keys: {list(top.keys())}")
        print(f"  document keys:  {list(doc_section.keys())}")
        for k, v in doc_section.items():
            if k != "blocks":
                print(f"  document.{k} (preview): {json.dumps(v)[:400]}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
