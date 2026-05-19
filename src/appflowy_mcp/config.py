import os
from dataclasses import dataclass


@dataclass
class Config:
    base_url: str
    email: str
    password: str
    tls_verify: bool
    transport: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Config":
        def required(key: str) -> str:
            value = os.getenv(key)
            if not value:
                raise RuntimeError(f"Missing required env var: {key}")
            return value

        transport = os.getenv("APPFLOWY_MCP_TRANSPORT", "stdio").lower()
        if transport not in ("stdio", "http"):
            raise RuntimeError(
                f"APPFLOWY_MCP_TRANSPORT must be 'stdio' or 'http', got: {transport}"
            )

        return cls(
            base_url=required("APPFLOWY_BASE_URL").rstrip("/"),
            email=required("APPFLOWY_BOT_EMAIL"),
            password=required("APPFLOWY_BOT_PASSWORD"),
            tls_verify=os.getenv("APPFLOWY_TLS_VERIFY", "true").lower()
            in ("true", "1", "yes"),
            transport=transport,
            host=os.getenv("APPFLOWY_MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("APPFLOWY_MCP_PORT", "8765")),
        )
