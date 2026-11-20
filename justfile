# CCAT local development tasks.
#
#   just ready-py    # sync the Python venv (devbox init hook)
#   just test        # Python test suite
#   just test-fe     # frontend (vitest) test suite
#   just run dev     # docker compose dev profile (live reload, :8443)
#   just run prod    # docker compose prod profile (detached, :443)

set export := true

# Initialize the python project environment
ready-py:
    uv lock
    uv sync --locked

# Run the full Python test suite
test:
    uv run pytest

# Install the frontend deps and run the frontend test suite
# (same install as the devbox init hook: jsdom 28 drops whatwg-encoding)
test-fe:
    npm install --prefix frontend --no-save --package-lock=false --no-audit --no-fund jsdom@28.1.0
    cd frontend && npx vitest run

# Run the deployment stack for one profile: just run dev | just run prod
run profile="dev":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ profile }}" in
        dev)
            exec docker compose --profile dev up --build
            ;;
        prod)
            # Caddy serves the SPA from ./frontend/dist, so build it first.
            (cd frontend && npm install --no-save --package-lock=false --no-audit --no-fund jsdom@28.1.0 && npm run build)
            exec docker compose --profile prod up -d --build
            ;;
        *)
            echo "unknown profile '{{ profile }}' (expected 'dev' or 'prod')" >&2
            exit 2
            ;;
    esac
