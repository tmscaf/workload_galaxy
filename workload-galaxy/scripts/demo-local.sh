#!/usr/bin/env bash
# Run the showcase on this machine: a simulated OpenShift cluster with real-looking failures, so you
# can see everything the board does without a cluster. Needs Docker; touches nothing else.
#
#   scripts/demo-local.sh                  # "A bad release day" (six deployments failing six ways)
#   scripts/demo-local.sh network          # or: infrastructure | cascade | baseline
#   scripts/demo-local.sh enterprise       # 600 projects, ~11,500 workloads, 390 nodes: how it scales
#   scripts/demo-local.sh stop             # remove it
#   PORT=9000 scripts/demo-local.sh        # another port (default 8099)
#
# Every scenario and preset is also in the Simulate menu of the page itself.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=workload-galaxy-demo
PORT="${PORT:-8099}"
PRESET="${1:-deploy-failures}"

if [ "${PRESET}" = "stop" ]; then
  docker rm -f "${NAME}" >/dev/null 2>&1 && echo "stopped ${NAME}" || echo "${NAME} was not running"
  exit 0
fi
[ "${PRESET}" = "baseline" ] && PRESET=""
SCALE=""
if [ "${PRESET}" = "enterprise" ]; then SCALE="enterprise-xl"; PRESET=""; fi

VERSION="$(tr -d ' \n' < VERSION)"
echo "==> building workload-galaxy:demo (${VERSION})"
docker build -q -t workload-galaxy:demo --build-arg GALAXY_VERSION="${VERSION}" --build-arg GALAXY_BUILD="demo" . >/dev/null

docker rm -f "${NAME}" >/dev/null 2>&1 || true
docker run -d --name "${NAME}" --restart unless-stopped -p "127.0.0.1:${PORT}:8080" \
  -e GALAXY_MODE=demo -e GALAXY_DEMO_PRESET="${PRESET}" -e GALAXY_DEMO_SCALE="${SCALE}" \
  -e GALAXY_REFRESH_SECONDS="$([ -n "${SCALE}" ] && echo 30 || echo 10)" workload-galaxy:demo >/dev/null

URL="http://127.0.0.1:${PORT}/?view=triage"
for _ in $(seq 1 90); do
  curl -fs "http://127.0.0.1:${PORT}/readyz" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fs "http://127.0.0.1:${PORT}/readyz" >/dev/null || { echo "demo did not start; see: docker logs ${NAME}"; exit 1; }
echo "==> demo is up: ${URL}"
echo "    Simulate (top bar) switches scenarios. Stop it with: scripts/demo-local.sh stop"
[ -n "${NO_OPEN:-}" ] || { command -v open >/dev/null 2>&1 && open "${URL}"; } || true
