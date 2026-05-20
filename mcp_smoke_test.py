"""Hit the MCP server through its HTTP transport with per-user auth headers."""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    load_dotenv()
    email = os.environ["APPFLOWY_BOT_EMAIL"]
    password = os.environ["APPFLOWY_BOT_PASSWORD"]
    url = os.environ.get("APPFLOWY_MCP_URL", "http://localhost:8765/mcp")
    headers = {
        "X-AppFlowy-Email": email,
        "X-AppFlowy-Password": password,
    }
    print(f"Connecting to {url} as {email}")
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"\n=== tools ({len(tools.tools)}) ===")
            for t in tools.tools:
                print(f"  - {t.name}")

            print("\n=== call list_workspaces ===")
            result = await session.call_tool("list_workspaces", {})
            for c in result.content:
                if hasattr(c, "text"):
                    try:
                        parsed = json.loads(c.text)
                        print(json.dumps(parsed, indent=2, ensure_ascii=False)[:1500])
                    except Exception:
                        print(c.text[:1500])

    print("\n--- missing-header negative test ---")
    try:
        async with streamablehttp_client(url, headers={}) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = await session.call_tool("list_workspaces", {})
                texts = [c.text for c in r.content if hasattr(c, "text")]
                print(f"isError={r.isError}; content={texts}")
    except Exception as exc:
        print(f"raised: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
