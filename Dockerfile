FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml setup.py README.md LICENSE ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

ENTRYPOINT ["python", "-m", "memoryguard.mcp_server"]
