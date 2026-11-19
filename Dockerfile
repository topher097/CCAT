# syntax=docker/dockerfile:1.7
#
# CCAT production image (plan §13).
#
# Three stages:
#   frontend       - builds the Vue SPA when frontend/package.json exists
#   python-builder - locked Python deps in a venv + the ccat wheel
#   runtime        - slim, non-root, LibreOffice-capable application image
#
# The wheel is installed outside the checkout, so missing
# templates, CWD-relative paths or a missing soffice fail in the image, not
# in production.  Configuration, templates and assets live under /app:
#
#   /app/config/ccat_docker.json   read-only season config (DB mode)
#   /app/data/                     document/email templates (read-only)
#   /app/alembic/                  migrations (ccat db-upgrade)
#   /app/frontend/dist/            compiled SPA (served by Caddy)
#   /data  /artifacts  /logs       writable declared volumes

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=24
ARG UV_VERSION=0.12.22

# --------------------------------------------------------------------------
# Stage 1: Vue SPA.  Skipped gracefully until the frontend lands: the stage
# always exists, but builds nothing when frontend/package.json is absent so
# the image keeps building pre-Phase-4.
# --------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS frontend

WORKDIR /build

# frontend/ is copied whole (node_modules/dist are .dockerignore'd) so the
# build works before and after the Vue app lands.
COPY frontend/ /build/

RUN set -eu; \
    if [ -f package.json ]; then \
        if [ -f package-lock.json ]; then npm ci; else npm install; fi; \
        npm run build; \
    else \
        echo "frontend/package.json absent: building without the SPA" >&2; \
        mkdir -p /build/dist; \
    fi

# --------------------------------------------------------------------------
# Stage 2: Python dependencies and the application wheel.
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS python-builder

ARG UV_VERSION
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# Keep the venv at the same path the runtime uses so console-script
# shebangs (#!/app/.venv/bin/python) stay valid after the copy.
WORKDIR /app

# Dependency layer first so source edits do not re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
RUN uv build --wheel --out-dir /dist \
 && uv pip install --python /app/.venv/bin/python --no-deps /dist/ccat-*.whl

# --------------------------------------------------------------------------
# Stage 3: runtime.
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG PYTHON_VERSION

# libreoffice-writer provides soffice for py3o .ott -> PDF rendering;
# matplotlib is headless (Agg) but its wheels still link libgl/libglib.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer \
      fonts-liberation \
      fonts-dejavu-core \
      libgl1 \
      libglib2.0-0 \
      ca-certificates \
      curl \
      tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CCAT_CONFIG=/app/config/ccat_docker.json \
    PATH="/app/.venv/bin:${PATH}"

# Non-root runtime user: owns only the app dir and the writable volumes.
RUN groupadd --gid 10001 ccat \
 && useradd --uid 10001 --gid ccat --home-dir /app \
      --shell /usr/sbin/nologin --no-create-home ccat

WORKDIR /app

COPY --from=python-builder /app/.venv /app/.venv
COPY --from=python-builder /usr/local/bin/uv /usr/local/bin/uv
COPY --from=python-builder /dist/ /tmp/wheel/

# Install the wheel in the runtime image (not from the checkout) and keep it
# for provenance/audit.
RUN uv pip install --python /app/.venv/bin/python --no-deps /tmp/wheel/ccat-*.whl \
 && mv /tmp/wheel/ccat-*.whl /app/ccat.whl \
 && rm -rf /tmp/wheel

COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY config/ccat_docker.json /app/config/ccat_docker.json
COPY data /app/data
COPY scripts/docker_entrypoint.sh /usr/local/bin/ccat-entrypoint
COPY --from=frontend /build/dist /app/frontend/dist

# The app derives its deployment root from the module path
# (``parents[3]`` of ``ccat/db/engine.py``), which for an installed wheel
# is the venv's ``lib/python3.12`` directory.  Link the runtime data roots
# there so migrations and the SPA resolve without a source checkout.
RUN ROOT="$(/app/.venv/bin/python -c 'from ccat.db.engine import ROOT; print(ROOT)')" \
 && ln -sfn /app/alembic "$ROOT/alembic" \
 && mkdir -p "$ROOT/frontend" \
 && ln -sfn /app/frontend/dist "$ROOT/frontend/dist"

RUN chmod 0755 /usr/local/bin/ccat-entrypoint \
 && mkdir -p /app/frontend /data /artifacts /logs \
 && chown -R 10001:10001 /app /data /artifacts /logs \
 && test -x /app/.venv/bin/ccat

USER 10001:10001

# Sanity: the installed package must see its migrations and templates.
RUN /app/.venv/bin/python -c "\
import pathlib, shutil; \
from ccat.db.engine import ALEMBIC_DIR; \
assert ALEMBIC_DIR.is_dir(), ALEMBIC_DIR; \
assert pathlib.Path('/app/data/child_information_sheet_2026.ott').is_file(); \
assert shutil.which('soffice'), 'soffice missing'; \
print('runtime self-check ok')"

EXPOSE 8650

ENTRYPOINT ["/usr/local/bin/ccat-entrypoint"]
CMD ["uvicorn", "ccat.api.main:app", "--host", "0.0.0.0", "--port", "8650"]
