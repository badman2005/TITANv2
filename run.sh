#!/usr/bin/env bash
# =====================================================================
# Start Project TITAN (API + web app [+ simulator]) — everything on ONE port.
#
#   ./run.sh                      http://127.0.0.1:8000
#   ./run.sh --https              self-signed HTTPS on all interfaces (needed for the camera on a phone)
#   ./run.sh --lan                plain HTTP on all interfaces (other devices on your network; no camera)
#   ./run.sh --simulate thermal   also start a simulator run on crate 1 once the API is up
#   ./run.sh --dev                auto-reload the backend on code changes
#   ./run.sh --port 9000
# Scenarios: normal thermal freeze tamper mkt offline offline_thermal   (see: python edge_simulator.py --list)
# =====================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

HTTPS=0; LAN=0; SIM=""; DEV=0; PORT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --https) HTTPS=1 ;; --lan) LAN=1 ;; --dev) DEV=1 ;;
    --simulate) SIM="${2:-normal}"; shift ;;
    --port) PORT_ARG="${2:?--port needs a value}"; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

[ -d .venv ] && [ -f .env ] || { echo "Run ./setup.sh first"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
set -a; source .env; set +a
PORT="${PORT_ARG:-${PORT:-8000}}"; export PORT

lan_ip() {
  { hostname -I 2>/dev/null | awk '{print $1}'; } || true
  [ "$(uname -s)" = "Darwin" ] && { ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true; }
}
LAN_IP="$(lan_ip | head -n1 || true)"

HOST="127.0.0.1"; SCHEME="http"; SSL_ARGS=()
if [ "$HTTPS" -eq 1 ] || [ "$LAN" -eq 1 ]; then HOST="0.0.0.0"; fi
if [ "$HTTPS" -eq 1 ]; then
  SCHEME="https"
  command -v openssl >/dev/null 2>&1 || { echo "openssl is required for --https"; exit 1; }
  if [ ! -f certs/titan.crt ] || [ ! -f certs/titan.key ]; then
    mkdir -p certs
    SAN="DNS:localhost,IP:127.0.0.1"; [ -n "$LAN_IP" ] && SAN="$SAN,IP:$LAN_IP"
    echo "Generating a self-signed certificate (valid 1 year) for $SAN"
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=titan.local" -addext "subjectAltName=$SAN" \
      -keyout certs/titan.key -out certs/titan.crt >/dev/null 2>&1
    chmod 600 certs/titan.key
  fi
  SSL_ARGS=(--ssl-keyfile certs/titan.key --ssl-certfile certs/titan.crt)
fi
SELF="${SCHEME}://127.0.0.1:${PORT}"; export TITAN_SELF_URL="$SELF"

RELOAD=(); [ "$DEV" -eq 1 ] && RELOAD=(--reload)
echo
echo "  TITAN is starting"
echo "    this machine : ${SCHEME}://localhost:${PORT}"
[ "$HOST" = "0.0.0.0" ] && [ -n "$LAN_IP" ] && echo "    other devices: ${SCHEME}://${LAN_IP}:${PORT}"
echo "    API docs     : ${SCHEME}://localhost:${PORT}/docs"
[ "$HTTPS" -eq 1 ] && echo "    (self-signed certificate: accept the browser warning once; the camera then works)"
[ ! -f frontend/dist/index.html ] && echo "    NOTE: web app not built — only the API is available (run ./setup.sh with Node 18+ installed)"
echo

uvicorn app.main:app --host "$HOST" --port "$PORT" "${SSL_ARGS[@]}" "${RELOAD[@]}" &
UV_PID=$!
SIM_PID=""
cleanup() { [ -n "$SIM_PID" ] && kill "$SIM_PID" 2>/dev/null || true; kill "$UV_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [ -n "$SIM" ]; then
  python - "$SELF" <<'PY' || { echo "API did not come up"; exit 1; }
import ssl, sys, time, urllib.request
ctx = ssl._create_unverified_context()
for _ in range(60):
    try:
        urllib.request.urlopen(sys.argv[1] + "/api/health", timeout=2, context=ctx); sys.exit(0)
    except Exception:
        time.sleep(0.5)
sys.exit(1)
PY
  INSECURE=(); [ "$HTTPS" -eq 1 ] && INSECURE=(--insecure)
  echo "  >> starting simulator: crate 1, scenario '$SIM'"
  python edge_simulator.py --api "$SELF" --crate-id 1 --scenario "$SIM" "${INSECURE[@]}" &
  SIM_PID=$!
fi
wait "$UV_PID"
