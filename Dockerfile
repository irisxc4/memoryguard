FROM python:3.12-slim

WORKDIR /app

ENV HOME=/home/memoryguard \
    MEMORYGUARD_HOME=/app/.glama-data

COPY pyproject.toml setup.py README.md LICENSE ./
COPY src/ ./src/

RUN groupadd --system memoryguard \
    && useradd --system --gid memoryguard --home-dir /home/memoryguard --create-home memoryguard \
    && pip install --no-cache-dir -e . \
    && mkdir -p /app/.glama-data \
    && chown -R memoryguard:memoryguard /app/.glama-data /home/memoryguard

USER memoryguard
ENTRYPOINT ["python", "-m", "memoryguard.mcp_server"]
