FROM python:3.11-slim

WORKDIR /app

# System deps for camel-oasis native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY sim/ sim/
COPY api/ api/
COPY cli.py VERSION ./
COPY personas/ personas/

# Install dependencies
RUN uv sync --frozen --no-dev

# Ensure data dir exists for SQLite
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
