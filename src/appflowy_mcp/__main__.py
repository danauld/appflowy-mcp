from dotenv import load_dotenv

from .config import Config
from .server import build_server


def main() -> None:
    load_dotenv()
    config = Config.from_env()
    if config.transport != "http":
        raise RuntimeError(
            "APPFLOWY_MCP_TRANSPORT must be 'http': per-user auth reads "
            "X-AppFlowy-Email/Password from HTTP headers and has no stdio path."
        )
    mcp, _pool = build_server(config)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
