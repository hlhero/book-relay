#!/usr/bin/env bash
# 在 GitHub Codespaces / 容器中启动 BookRelay（幂等，可重复执行）
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
export PORT
LOG="${BOOKRELAY_LOG:-/tmp/bookrelay.log}"
BASE_PY="$(command -v python3 || command -v python)"
VENV=".venv"

probe() {
  "$BASE_PY" - <<'EOF' >/dev/null 2>&1
import os
import urllib.request

urllib.request.urlopen("http://127.0.0.1:%s/" % os.environ["PORT"], timeout=2).read(1)
EOF
}

if probe; then
  echo "[bookrelay] already running on port ${PORT}"
  exit 0
fi

if [ ! -x "${VENV}/bin/python" ]; then
  echo "[bookrelay] creating virtualenv at ${VENV}"
  "$BASE_PY" -m venv "${VENV}"
fi
PY="${VENV}/bin/python"

if ! "$PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[bookrelay] installing dependencies..."
  "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt
fi

export BOOKRELAY_ENV="${BOOKRELAY_ENV:-development}"
echo "[bookrelay] starting uvicorn on 0.0.0.0:${PORT} (env=${BOOKRELAY_ENV})"
nohup "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers >"${LOG}" 2>&1 &

for _ in $(seq 1 40); do
  if probe; then
    echo "[bookrelay] ready on port ${PORT}"
    exit 0
  fi
  sleep 1
done

echo "[bookrelay] startup failed, last log lines:" >&2
tail -n 40 "${LOG}" >&2 || true
exit 1
