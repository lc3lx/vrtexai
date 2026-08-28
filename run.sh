#!/usr/bin/env bash
#
# Bring the Vertex web app up on this machine.
#
#   ./run.sh              start it (sets everything up on the first run)
#   PORT=8080 ./run.sh    on a different port
#   ./run.sh --setup      prepare only: venv, dependencies, .env — do not start
#
# Safe to run repeatedly: it creates what is missing and leaves the rest alone.
# It never overwrites an existing .env, because that is where the secrets live.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$PWD"
BACKEND="$ROOT/backend"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"

PORT="${PORT:-7009}"
# Bind to every interface so the port is reachable from outside the box. Put it
# behind a reverse proxy with TLS before real customers use it — see the note
# printed at the end.
HOST="${HOST:-0.0.0.0}"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m/!\\\033[0m %s\n' "$*"; }
die()  { printf '\033[31mx\033[0m %s\n' "$*" >&2; exit 1; }

# --- python ----------------------------------------------------------------
command -v python3 >/dev/null || die "python3 is not installed.  apt install python3 python3-venv"

if [ ! -x "$PY" ]; then
  say "creating the virtual environment"
  python3 -m venv "$VENV" 2>/dev/null || die \
    "could not create a venv.  apt install python3-venv"
fi

# Reinstall only when requirements.txt is newer than the last successful install.
STAMP="$VENV/.requirements-stamp"
if [ ! -f "$STAMP" ] || [ "$BACKEND/requirements.txt" -nt "$STAMP" ]; then
  say "installing dependencies"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r "$BACKEND/requirements.txt"
  touch "$STAMP"
fi

# python-magic needs the system library; the wheel alone is not enough on Linux.
if ! "$PY" -c "import magic" 2>/dev/null; then
  warn "libmagic is missing — uploads will be rejected.  apt install -y libmagic1"
fi

# --- configuration ---------------------------------------------------------
ENV_FILE="$BACKEND/.env"
if [ ! -f "$ENV_FILE" ]; then
  say "writing backend/.env with a freshly generated JWT_SECRET"
  cp "$BACKEND/.env.example" "$ENV_FILE"
  SECRET="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  # The delimiter is | because a generated secret can contain a slash.
  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$SECRET|" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  warn "AI_PROVIDER is 'local' by default. For a hosted model set AI_PROVIDER=openrouter"
  warn "and OPENROUTER_API_KEY in backend/.env."
fi
grep -q '^JWT_SECRET=.\+' "$ENV_FILE" || die "JWT_SECRET is empty in backend/.env"

# --- mongodb ---------------------------------------------------------------
MONGO_URL="$(grep -E '^MONGO_URL=' "$ENV_FILE" | cut -d= -f2- || true)"
MONGO_URL="${MONGO_URL:-mongodb://localhost:27017}"
if ! "$PY" - "$MONGO_URL" <<'PYEOF' 2>/dev/null
import sys, socket
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
host, port = url.hostname or "localhost", url.port or 27017
socket.create_connection((host, port), timeout=3).close()
PYEOF
then
  die "MongoDB is not reachable at $MONGO_URL

  Install it:   apt install -y mongodb-org   (or use a hosted URL)
  Start it:     systemctl enable --now mongod
  Or point MONGO_URL in backend/.env at your own server."
fi

# --- the reader ------------------------------------------------------------
# The OCR pipeline is the desktop product's code and is not part of this
# repository. Without it the app runs and accepts uploads, but every job fails.
WORKER="$(grep -E '^WORKER_ROOT=' "$ENV_FILE" | cut -d= -f2- || true)"
WORKER="${WORKER:-../../ExcelCleaner/ocr_worker}"
case "$WORKER" in /*) WORKER_ABS="$WORKER" ;; *) WORKER_ABS="$BACKEND/$WORKER" ;; esac
if [ ! -d "$WORKER_ABS" ]; then
  warn "WORKER_ROOT does not exist: $WORKER_ABS"
  warn "The app will start and accept uploads, but processing will fail until the"
  warn "ocr_worker directory is on this machine and WORKER_ROOT points at it."
fi

mkdir -p "$BACKEND/storage"

if [ "${1:-}" = "--setup" ]; then
  say "setup complete — not starting"
  exit 0
fi

# --- run -------------------------------------------------------------------
# One worker on purpose: jobs are queued in-process behind a semaphore, so a
# second worker would run its own queue and both would call the reader at once.
#
# No --reload: it is for development, and on some platforms it puts the server
# on an event loop that cannot start the reader's child process at all.
say "starting on http://$HOST:$PORT"
echo
echo "    public page     http://<this-server>:$PORT/"
echo "    application     http://<this-server>:$PORT/app"
echo "    api docs        http://<this-server>:$PORT/docs"
echo
echo "    On a first run the administrator password is printed below, once."
echo "    Behind CloudPanel, reverse-proxy your domain to 127.0.0.1:$PORT and"
echo "    set HOST=127.0.0.1 so the port is not exposed directly."
echo

cd "$BACKEND"
exec "$VENV/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" --workers 1
