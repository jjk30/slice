# slice gateway image (phase 15; phase 21 bundles the built dashboard).
#
# Stage 1: build the Vue dashboard to static files. package*.json is copied first so the
# npm install layer is cached and a source-only change never reinstalls node_modules.
FROM node:22-alpine AS dashboard
WORKDIR /dash
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# Stage 2: the FastAPI app, run with uvicorn app.main:app on port 8080.
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

# sentence-transformers pulls in torch. On this arm64 (aarch64) CPU-only box the default
# torch wheel drags in NVIDIA CUDA libraries the image can never use, ballooning it by
# gigabytes. Install the CPU-only wheel FIRST from the PyTorch CPU index (it serves
# linux/aarch64 cp312 wheels); the requirements install below then finds torch already
# satisfied and leaves it in place instead of pulling the CUDA build.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# Copy the dependency manifest FIRST and install, so this layer is cached and a
# code-only change never reinstalls the (heavy) dependency tree.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Now the runtime code. Only what the app needs at run time: tests, dashboard,
# .venv, .git, .env, rag_store etc. are kept out by .dockerignore.
COPY app/ ./app/
COPY migrations/ ./migrations/
# GUARDRAILS_CONFIG_DIR defaults to the relative "guardrails" path, so the config
# tree must ship in the image or RailsConfig.from_path fails and build_engine
# returns None (guardrails silently off) in every container.
COPY guardrails/ ./guardrails/
# The built dashboard from stage 1. The gateway serves it at "/" (DASHBOARD_DIST in
# app/main.py looks for /app/dashboard/dist); the source never enters this stage.
COPY --from=dashboard /dash/dist ./dashboard/dist
COPY pyproject.toml ./

# Drop privileges: run as a non-root user that owns the app dir.
RUN useradd --create-home --uid 10001 slice \
    && chown -R slice:slice /app
USER slice

EXPOSE 8080

# 0.0.0.0 (not 127.0.0.1) so the port is reachable from outside the container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
