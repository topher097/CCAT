"""Static deployment-packaging checks (plan §13, Phase 5).

No Docker daemon required: these assertions catch the packaging mistakes
that would only surface on a deploy host — secrets in the wrong layer,
missing services/profiles, a bypassed SPA fallback, or a non-root image.
"""

import json
import re
from pathlib import Path

from ccat.config import load_config

ROOT = Path(__file__).resolve().parent.parent

DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
CADDY = (ROOT / "Caddyfile").read_text(encoding="utf-8")
CADDY_DEV = (ROOT / "Caddyfile.dev").read_text(encoding="utf-8")
ENVRC = (ROOT / ".envrc").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
ENTRYPOINT = (ROOT / "scripts" / "docker_entrypoint.sh").read_text(
    encoding="utf-8"
)
DEPLOYMENT = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
CUTOVER = (ROOT / "docs" / "cutover.md").read_text(encoding="utf-8")


def _service_block(name: str) -> str:
    """Return the compose text of one service (anchors/indent aware).

    Args:
        name: Compose service key.

    Returns:
        The service body, indented two spaces under ``services:``.
    """
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|^\S)",
        COMPOSE,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, f"service {name} missing from docker-compose.yml"
    return match.group(0)


def test_dockerfile_is_multistage_with_conditional_frontend() -> None:
    """Frontend, builder and runtime stages exist; SPA build is optional."""
    assert "AS frontend" in DOCKERFILE
    assert "AS python-builder" in DOCKERFILE
    assert "AS runtime" in DOCKERFILE
    assert "[ -f package.json ]" in DOCKERFILE
    assert "npm ci" in DOCKERFILE
    assert "npm install" in DOCKERFILE
    assert "uv sync --frozen --no-dev" in DOCKERFILE
    assert "uv build --wheel" in DOCKERFILE


def test_dockerfile_runtime_dependencies_and_nonroot() -> None:
    """LibreOffice, fonts, headless libs and a fixed non-root uid."""
    assert "libreoffice-writer" in DOCKERFILE
    assert "fonts-liberation" in DOCKERFILE
    assert "fonts-dejavu-core" in DOCKERFILE
    assert "libgl1" in DOCKERFILE
    assert "curl" in DOCKERFILE
    assert "useradd --uid 10001" in DOCKERFILE
    assert "USER 10001:10001" in DOCKERFILE
    # The wheel is installed outside the checkout and the alembic/SPA roots
    # are linked where the installed package derives its ROOT.
    assert "ln -sfn /app/alembic" in DOCKERFILE
    assert "ln -sfn /app/frontend/dist" in DOCKERFILE
    assert "ccat.whl" in DOCKERFILE


def test_dockerfile_keeps_healthcheck_out_of_the_image() -> None:
    """The image stays generic; compose owns the API healthcheck."""
    assert "HEALTHCHECK" not in DOCKERFILE


def test_compose_services_profiles_and_commands() -> None:
    """Both profiles resolve to the documented service set."""
    prod = {"api", "worker", "db-upgrade", "caddy", "opencode"}
    dev = {"api-dev", "worker", "db-upgrade", "web", "caddy-dev", "opencode"}
    for name in prod:
        assert '"prod"' in _service_block(name), name
    for name in dev:
        assert '"dev"' in _service_block(name), name
    assert '"ccat", "worker", "--config"' in _service_block("worker")
    assert "uvicorn" in _service_block("api")
    assert "--reload" not in _service_block("api")
    assert "--reload" in _service_block("api-dev")
    assert "--reload" not in _service_block("worker")


def test_compose_db_upgrade_is_one_shot_and_shared_db_volume() -> None:
    """Migrations run once, before api/worker, on the shared DB volume."""
    db_upgrade = _service_block("db-upgrade")
    assert '"ccat", "db-upgrade", "--config"' in db_upgrade
    assert 'restart: "no"' in db_upgrade
    assert "ccat-db:/data" in COMPOSE
    assert (
        "./config/ccat_docker.json:/app/config/ccat_docker.json:ro" in COMPOSE
    )
    for name in ("api", "worker"):
        block = _service_block(name)
        assert "volumes: *app-volumes" in block, name
        assert "condition: service_completed_successfully" in block, name


def test_compose_volumes_ports_and_logging() -> None:
    """Declared volumes, Caddy-only ingress and log rotation."""
    for volume in (
        "ccat-db:",
        "ccat-artifacts:",
        "ccat-logs:",
        "caddy-data:",
        "caddy-config:",
        "opencode-data:",
    ):
        assert re.search(rf"^  {re.escape(volume)}$", COMPOSE, re.MULTILINE)
    caddy = _service_block("caddy")
    assert '"80:80"' in caddy
    assert '"443:443"' in caddy
    assert "./frontend/dist:/srv/frontend:ro" in caddy
    assert "max-size" in COMPOSE and "max-file" in COMPOSE
    # Only Caddy binds host ports.
    for name in ("api", "worker", "db-upgrade", "web", "api-dev", "opencode"):
        assert "ports:" not in _service_block(name), name


def test_compose_opencode_server_backs_ai_screening() -> None:
    """The opencode service is built from upstream and wired to the app."""
    block = _service_block("opencode")
    assert "github.com/roborew/opencode-server" in block
    assert "opencode-data:/var/lib/opencode-data" in block
    # Host credentials are copied into the container before it drops root.
    assert "/run/opencode-auth.json" in block
    assert "install -m 600" in block
    # The app and the server share one URL and one basic-auth password.
    assert "OPENCODE_BASE_URL: ${OPENCODE_BASE_URL:-http://opencode:4097}" in (
        COMPOSE
    )
    assert "OPENCODE_SERVER_PASSWORD: ${OPENCODE_SERVER_PASSWORD:-}" in COMPOSE
    # The worker waits for the LLM server before it takes a screening job.
    assert "opencode:" in _service_block("worker")
    assert "condition: service_healthy" in _service_block("worker")


def test_compose_has_no_hardcoded_secrets() -> None:
    """Every secret-shaped env value is compose interpolation."""
    for variable in (
        "ZITE_API_KEY",
        "ZEROBOUNCE_API_KEY",
        "CCAT_SESSION_SECRET",
        "OIDC_CLIENT_SECRET",
        "OPENCODE_SERVER_PASSWORD",
    ):
        for match in re.finditer(rf"{variable}\s*:\s*(\S+)", COMPOSE):
            assert match.group(1).startswith("${"), (variable, match.group(0))
    for text in (DOCKERFILE, CADDY, CADDY_DEV):
        assert "ZITE_API_KEY" not in text
        assert "ZEROBOUNCE_API_KEY" not in text
        assert "CCAT_SESSION_SECRET" not in text
    # Interpolated values are not secrets; anything left over must be empty.
    literal = re.sub(r"\$\{[^}]*\}", "", COMPOSE)
    assert not re.search(r"password[ \t]*[:=][ \t]*\S", literal, re.IGNORECASE)


def test_caddyfile_same_origin_spa_and_api_routes() -> None:
    """One hostname: /api proxied to the API, everything else is the SPA."""
    assert "/api/*" in CADDY
    assert "reverse_proxy api:8650" in CADDY
    assert "try_files {path} /index.html" in CADDY
    assert "root * /srv/frontend" in CADDY
    assert "{$CCAT_DOMAIN:localhost}" in CADDY
    assert "tls {$CCAT_ACME_EMAIL:internal}" in CADDY
    assert "tls internal" in CADDY_DEV
    assert "reverse_proxy web:5173" in CADDY_DEV
    assert "reverse_proxy api-dev:8650" in CADDY_DEV


def test_envrc_holds_public_settings_and_points_at_private_secrets() -> None:
    """Direnv carries the deployment settings; secrets stay in .envrc.private."""
    for variable in (
        "CCAT_DOMAIN",
        "CCAT_ACME_EMAIL",
        "CCAT_IMAGE_TAG",
        "OPENCODE_BASE_URL",
        "OPENCODE_AUTH_FILE",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
    ):
        assert re.search(rf"^export {variable}=", ENVRC, re.MULTILINE), variable
    assert "source_env .envrc.private" in ENVRC
    # No secret value in the committed file: the secret names may be named in
    # the pointer comment, but never assigned.
    for secret in (
        "CCAT_SESSION_SECRET",
        "ZITE_API_KEY",
        "ZEROBOUNCE_API_KEY",
        "OPENCODE_SERVER_PASSWORD",
        "OIDC_CLIENT_SECRET",
    ):
        assert not re.search(rf"^export {secret}=", ENVRC, re.MULTILINE), secret


def test_dockerignore_excludes_state_and_secrets() -> None:
    """Context excludes VCS, envs, node_modules, output and secrets."""
    entries = set(DOCKERIGNORE.split())
    for entry in (
        ".git",
        ".venv",
        "node_modules",
        "output",
        "temp",
        "frontend/dist",
        ".env",
    ):
        assert entry in entries, entry


def test_entrypoint_waits_runs_optional_migration_and_execs() -> None:
    """Entrypoint is POSIX sh, waits for the DB dir, then execs the command."""
    assert ENTRYPOINT.startswith("#!/bin/sh")
    assert "set -eu" in ENTRYPOINT
    assert "CCAT_RUN_MIGRATIONS" in ENTRYPOINT
    assert "ccat db-upgrade" in ENTRYPOINT
    assert 'exec "$@"' in ENTRYPOINT
    assert "sleep 1" in ENTRYPOINT


def test_docker_config_is_db_mode_with_absolute_paths() -> None:
    """The deployment config selects DB mode and container paths."""
    cfg = load_config(str(ROOT / "config" / "ccat_docker.json"))
    assert cfg.storage.mode == "db"
    assert cfg.storage.database_url.startswith("sqlite+aiosqlite:////data/")
    assert cfg.storage.artifact_root.startswith("/artifacts")
    assert cfg.log_file.startswith("/logs/")
    assert cfg.info_sheet.template_file.startswith("/app/data/")
    assert cfg.api.host == "0.0.0.0"
    assert cfg.api.port == 8650
    raw = json.loads(
        (ROOT / "config" / "ccat_docker.json").read_text(encoding="utf-8")
    )
    assert raw["storage"]["mode"] == "db"


def test_docs_contain_operational_runbooks() -> None:
    """Operator docs cover backup/restore, rollback, TLS and the checklist."""
    for heading in (
        "Backup and restore",
        "Upgrade and rollback",
        "Security checklist",
        "LibreOffice dependency",
    ):
        assert heading in DEPLOYMENT, heading
    assert "Never copy a live SQLite file" in DEPLOYMENT
    assert "sqlite3.connect" in DEPLOYMENT
    assert "Let's Encrypt" in DEPLOYMENT
    for heading in (
        "Freeze writers",
        "Back up the sources of truth",
        "Rollback",
        "reverse",
        "workbook-export",
    ):
        assert heading in CUTOVER, heading
    assert "Post-cutover verification checklist" in CUTOVER
    assert "Rehearsal result" in CUTOVER
