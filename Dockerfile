# slice gateway image (phase 15).
# FastAPI app run with uvicorn app.main:app on port 8080.
FROM python:3.12-slim

# Faster, quieter Python in a container: no .pyc files, unbuffered stdout/stderr
# (so uvicorn logs stream straight to `docker logs`), and no pip version chatter.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is here for any dependency in the pinned tree that lacks a
# cp312 manylinux wheel and has to compile from an sdist. apt lists are removed
# in the same layer so they never ship in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the dependency manifest FIRST and install, so this layer is cached and a
# code-only change never reinstalls the (heavy) dependency tree.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Now the runtime code. Only what the app needs at run time — tests, dashboard,
# .venv, .git, .env, rag_store etc. are kept out by .dockerignore.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY pyproject.toml ./

# Drop privileges: run as a non-root user that owns the app dir.
RUN useradd --create-home --uid 10001 slice \
    && chown -R slice:slice /app
USER slice

EXPOSE 8080

# 0.0.0.0 (not 127.0.0.1) so the port is reachable from outside the container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
