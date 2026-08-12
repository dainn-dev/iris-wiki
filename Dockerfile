# syntax=docker/dockerfile:1

# ---------- Stage 1 : build the Workbench web bundle ----------
FROM node:22-slim AS web-build
WORKDIR /src/frontend

# Install frontend deps first (layer-cache friendly)
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

# Copy sources and build. vite outDir is ../openkb/web per vite.config.
COPY frontend/ .
RUN npm run build

# ---------- Stage 2 : python wheel incl. bundled web assets ----------
# Build the wheel from source so hatchling picks up openkb/web via `artifacts`.
FROM python:3.12-slim AS build
WORKDIR /src

# hatch-vcs needs the git binary to read the version from the copied .git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY .git .git
COPY pyproject.toml README.md ./
COPY openkb/ openkb/
COPY skills/ skills/
COPY --from=web-build /src/openkb/web/ openkb/web/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir build \
    && python -m build --wheel

# ---------- Stage 3 : runtime ----------
FROM python:3.12-slim AS runtime

# So the openkb CLI / API opens run directly.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work

# Copy the wheel and install with the web extra (fastapi/uvicorn).
COPY --from=build /src/dist/*.whl /tmp/
RUN WHEEL="$(ls /tmp/*.whl)" \
    && pip install --no-cache-dir "$WHEEL[web]" \
    && rm -rf /tmp/*.whl

# Non-interactive KB initializer (see docker/init_kb.py). Lets `openkb init`
# run against a mounted, empty /work without a TTY.
COPY docker/init_kb.py /usr/local/bin/openkb-init
RUN chmod +x /usr/local/bin/openkb-init

# Entrypoint: auto-registers a KB mounted at /work (if present) so the REST API
# lists it, then execs the given command. Keeps CLI usage working too
# (docker run <image> openkb add /work → exec runs `openkb`).
COPY docker/entrypoint.sh /usr/local/bin/openkb-entrypoint
RUN chmod +x /usr/local/bin/openkb-entrypoint

ENTRYPOINT ["openkb-entrypoint"]

# KB data lives under /work by default.
VOLUME ["/work"]

EXPOSE 7566

# Default: serve the REST API + Workbench web UI on 0.0.0.0.
# Override CMD to run the CLI, e.g.:
#   docker run --rm <image> openkb init
#   docker run --rm <image> openkb add paper.pdf
CMD ["openkb-web", "--host", "0.0.0.0", "--port", "7566"]