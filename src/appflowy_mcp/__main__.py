from dotenv import load_dotenv

from .config import Config
from .server import build_server


def main() -> None:
    # Optional .env for dev. In docker/MCP-config-launch env vars come from outside.
    load_dotenv()
    config = Config.from_env()
    mcp, _client = build_server(config)
    if config.transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
