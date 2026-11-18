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
  if [ "${BOOKRELAY_RESTART:-0}" != "1" ]; then
    echo "[bookrelay] already running on port ${PORT} (set BOOKRELAY_RESTART=1 to restart)"
    exit 0
  fi
  pid="$(ss -lntp 2>/dev/null | grep -E ":${PORT}[[:space:]]" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1)"
  if [ -n "${pid}" ]; then
    echo "[bookrelay] restarting (pid ${pid})"
    kill "${pid}" || true
    for _ in $(seq 1 10); do
      probe || break
      sleep 1
    done
  fi
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

# Codespaces 的端口转发会在代理层改写 Host / 终止 TLS，导致 Origin 与 Host、scheme
# 对不上而被 CSRF 校验拒绝。这里把当前转发域名显式加入可信来源。
if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
  origin_main="https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  origin_preview="https://${CODESPACE_NAME}-${PORT}.preview.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  export BOOKRELAY_TRUSTED_ORIGINS="${BOOKRELAY_TRUSTED_ORIGINS:-},${origin_main},${origin_preview}"
  echo "[bookrelay] trusted origin: ${origin_main}"
fi

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
