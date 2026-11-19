#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
ROOT_DIR="${PROJECT_DIR:h}"
NO_BROWSER=0
SETUP=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --setup) SETUP=1 ;;
    --no-browser) NO_BROWSER=1 ;;
    --check-health) CHECK_ONLY=1 ;;
    *) print -u2 "未知参数：$arg"; exit 2 ;;
  esac
done

LOCAL_PYTHON="$PROJECT_DIR/.venv/bin/python"
PYTHON_BIN=""
if [[ -x "$LOCAL_PYTHON" ]]; then
  PYTHON_BIN="$LOCAL_PYTHON"
elif (( SETUP )); then
  BOOTSTRAP_PYTHON=""
  if [[ -n "${TRADING_LAB_PYTHON:-}" && -x "${TRADING_LAB_PYTHON}" ]]; then
    BOOTSTRAP_PYTHON="$TRADING_LAB_PYTHON"
  elif [[ -x "$ROOT_DIR/.venv311/bin/python" ]]; then
    BOOTSTRAP_PYTHON="$ROOT_DIR/.venv311/bin/python"
  elif command -v python3.11 >/dev/null 2>&1; then
    BOOTSTRAP_PYTHON="$(command -v python3.11)"
  fi
  if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
    print -u2 "未找到 Python 3.11。请安装 Python 3.11 后再运行 --setup。"
    exit 1
  fi
  if ! "$BOOTSTRAP_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    print -u2 "Trading Lab 需要 Python 3.11 或更高版本。"
    exit 1
  fi
  "$BOOTSTRAP_PYTHON" -m venv "$PROJECT_DIR/.venv"
  PYTHON_BIN="$LOCAL_PYTHON"
elif [[ -n "${TRADING_LAB_PYTHON:-}" && -x "${TRADING_LAB_PYTHON}" ]]; then
  PYTHON_BIN="$TRADING_LAB_PYTHON"
else
  print -u2 "未找到 Trading Lab 独立环境。请先运行 launch.command --setup。"
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  print -u2 "Trading Lab 需要 Python 3.11 或更高版本。"
  exit 1
fi

run_package_command() (
  local variable value
  local -a unset_proxy_vars
  unset_proxy_vars=()
  for variable in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    value="${(P)variable:-}"
    if [[ "$value" == socks5://* || "$value" == socks5h://* ]]; then
      unset_proxy_vars+=(-u "$variable")
    fi
  done
  env "${unset_proxy_vars[@]}" "$@"
)

if (( SETUP )); then
  run_package_command "$PYTHON_BIN" -m pip install -r "$PROJECT_DIR/requirements.lock"
  (cd "$PROJECT_DIR/frontend" && run_package_command npm ci && run_package_command npm run build)
  print "设置完成。可双击 launch.command 启动 Trading Lab。"
  exit 0
fi

HEALTH_STATE="$("$PYTHON_BIN" - <<'PY'
import json, socket, sys
from urllib.request import ProxyHandler, build_opener

try:
    with socket.create_connection(("127.0.0.1", 8090), timeout=0.4):
        pass
except OSError:
    print("free")
    sys.exit(0)
try:
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:8090/api/health", timeout=0.8) as response:
        body = json.loads(response.read(4096).decode("utf-8"))
    print("healthy" if body.get("service") == "tradinglab" and body.get("status") == "ok" else "busy")
except Exception:
    print("busy")
PY
)"

if (( CHECK_ONLY )); then
  case "$HEALTH_STATE" in
    healthy) print "Trading Lab 已在 http://127.0.0.1:8090/ 运行。" ;;
    free) print "本机 8090 端口空闲。" ;;
    *) print -u2 "本机 8090 端口正在被其他服务使用；未停止任何进程。"; exit 1 ;;
  esac
  exit 0
fi

if [[ "$HEALTH_STATE" == "healthy" ]]; then
  print "Trading Lab 已在 http://127.0.0.1:8090/ 运行。"
  if (( ! NO_BROWSER )); then open "http://127.0.0.1:8090/" >/dev/null 2>&1 || true; fi
  exit 0
elif [[ "$HEALTH_STATE" == "busy" ]]; then
  print -u2 "本机 8090 端口正在被其他服务使用；未停止任何进程。"
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/frontend/dist/index.html" ]]; then
  print -u2 "界面尚未构建。请先运行 launch.command --setup。"
  exit 1
fi

if (( NO_BROWSER )); then
  cd "$ROOT_DIR"
  exec "$PYTHON_BIN" -m trading_lab --host 127.0.0.1 --port 8090 --no-browser
else
  cd "$ROOT_DIR"
  exec "$PYTHON_BIN" -m trading_lab --host 127.0.0.1 --port 8090
fi
