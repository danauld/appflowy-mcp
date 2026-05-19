FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

ENV APPFLOWY_MCP_TRANSPORT=http \
    APPFLOWY_MCP_HOST=0.0.0.0 \
    APPFLOWY_MCP_PORT=8765

EXPOSE 8765

CMD ["appflowy-mcp"]
