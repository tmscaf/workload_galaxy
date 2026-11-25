#!/usr/bin/env bash
# One private file on the cluster holds every secret the suite needs; this script turns it into the
# Kubernetes Secrets the modules read. Nothing secret lives in the repository or on a laptop.
#
# It runs ON the cluster machine (it needs kubectl there, and bash 4+). From somewhere else:
#
#   ssh <cluster> 'bash -s -- init' < tools/cluster-secrets.sh     # first time: build the file
#   ssh <cluster> 'bash -s -- sync' < tools/cluster-secrets.sh     # file -> Kubernetes Secrets
#   ssh <cluster> 'bash -s -- list' < tools/cluster-secrets.sh     # which values are set (never the values)
#
#   init   creates the file if it is missing and fills in what it can: values already held by the
#          cluster's Secrets are copied in, the ones the suite invents (admin password, settings
#          token, agent token) are generated, and anything else is left empty for you to fill in. Never
#          overwrites a value that is already in the file.
#   sync   writes each Secret whose values are all present, skips namespaces that do not exist
#          yet, and restarts a workload only when its Secret actually changed (--no-restart: never).
#   list   prints each name with "set" or "empty".
#
# The file: ${GALAXY_SECRETS_FILE:-~/.config/workload-galaxy/secrets.env}, mode 600, one NAME=value
# per line. Everything after the first "=" is the value, exactly as written: no quotes, no escaping.
# To change a secret, edit the file and run sync.
set -euo pipefail

FILE="${GALAXY_SECRETS_FILE:-$HOME/.config/workload-galaxy/secrets.env}"

# name in the file | what it is
NAMES=(
  "GALAXY_ADMIN_PASSWORD|the password for the suite's Settings area (generated; change it to one you will remember)"
  "GALAXY_SETTINGS_TOKEN|shared by the suite and its modules so settings can be changed from the suite (generated)"
  "ESXI_USER|read-only account on the ESXi host"
  "ESXI_PASSWORD|its password"
  "NTOPNG_USER|read-only ntopng login on the router"
  "NTOPNG_PASSWORD|its password"
  "UNIFI_USER|view-only admin on the UniFi controller"
  "UNIFI_PASSWORD|its password"
  "NETGALAXY_ADMIN_PASSWORD|the Network module's password for blocking and unblocking"
  "NETGALAXY_BLOCKLIST_TOKEN|must match the token on the router (its sync script presents it)"
  "K8S_AGENT_TOKEN|shared by the Kubernetes module's collector and its node agents"
)
GENERATED="GALAXY_ADMIN_PASSWORD GALAXY_SETTINGS_TOKEN K8S_AGENT_TOKEN"

# namespace | Secret | key=NAME,key=NAME | workloads to restart when it changes
SECRETS=(
  "workload-galaxy-shell|workload-galaxy-shell-settings|admin-password=GALAXY_ADMIN_PASSWORD,settings-token=GALAXY_SETTINGS_TOKEN|deploy/workload-galaxy-shell"
  "esxi-galaxy|esxi-galaxy-settings|token=GALAXY_SETTINGS_TOKEN|deploy/esxi-galaxy"
  "esxi-galaxy|esxi-galaxy-host|user=ESXI_USER,password=ESXI_PASSWORD|deploy/esxi-galaxy"
  "network-galaxy|network-galaxy-ntopng|user=NTOPNG_USER,password=NTOPNG_PASSWORD|deploy/network-galaxy"
  "network-galaxy|network-galaxy-unifi|user=UNIFI_USER,password=UNIFI_PASSWORD|deploy/network-galaxy"
  "network-galaxy|network-galaxy-actions|admin-password=NETGALAXY_ADMIN_PASSWORD,blocklist-token=NETGALAXY_BLOCKLIST_TOKEN|deploy/network-galaxy"
  "workload-galaxy|galaxy-agent-token|token=K8S_AGENT_TOKEN|deploy/workload-galaxy ds/galaxy-agent"
)

declare -A VALUE=()

read_file() {
  VALUE=()
  [ -f "${FILE}" ] || return 0
  local line name
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in ''|'#'*) continue ;; esac
    name="${line%%=*}"
    [ "${name}" != "${line}" ] || continue
    VALUE["${name}"]="${line#*=}"
  done < "${FILE}"
}

write_file() {
  local dir tmp entry name what
  dir="$(dirname "${FILE}")"
  mkdir -p "${dir}" && chmod 700 "${dir}"
  tmp="$(mktemp "${dir}/.secrets.XXXXXX")"
  chmod 600 "${tmp}"
  {
    echo "# Workload Galaxy secrets for this cluster. This file is the only copy: it is in no repository."
    echo "# One NAME=value per line; everything after the first = is the value, exactly as written."
    echo "# After changing anything, run: tools/cluster-secrets.sh sync"
    for entry in "${NAMES[@]}"; do
      name="${entry%%|*}"; what="${entry#*|}"
      echo
      echo "# ${what}"
      echo "${name}=${VALUE[${name}]:-}"
    done
  } > "${tmp}"
  mv "${tmp}" "${FILE}"
}

in_cluster() {   # namespace secret key -> the value held by the cluster now, or nothing
  kubectl -n "$1" get secret "$2" -o "go-template={{index .data \"$3\"}}" 2>/dev/null | base64 -d 2>/dev/null || true
}

cmd_init() {
  umask 077
  read_file
  local spec ns secret pairs pair key name got copied=0 made=0 empty=()
  for spec in "${SECRETS[@]}"; do
    IFS='|' read -r ns secret pairs _ <<<"${spec}"
    IFS=',' read -ra list <<<"${pairs}"
    for pair in "${list[@]}"; do
      key="${pair%%=*}"; name="${pair#*=}"
      [ -z "${VALUE[${name}]:-}" ] || continue
      got="$(in_cluster "${ns}" "${secret}" "${key}")"
      if [ -n "${got}" ]; then VALUE["${name}"]="${got}"; copied=$((copied + 1)); fi
    done
  done
  for name in ${GENERATED}; do
    if [ -z "${VALUE[${name}]:-}" ]; then
      if [ "${name}" = "GALAXY_ADMIN_PASSWORD" ]; then VALUE["${name}"]="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
      else VALUE["${name}"]="$(openssl rand -hex 32)"; fi
      made=$((made + 1))
    fi
  done
  write_file
  for spec in "${NAMES[@]}"; do name="${spec%%|*}"; [ -n "${VALUE[${name}]:-}" ] || empty+=("${name}"); done
  echo "${FILE}: ${copied} copied from the cluster's Secrets, ${made} generated, ${#empty[@]} still empty${empty:+ (${empty[*]})}"
}

cmd_list() {
  read_file
  [ -f "${FILE}" ] || { echo "${FILE} does not exist yet: run init"; return 1; }
  local entry name
  for entry in "${NAMES[@]}"; do
    name="${entry%%|*}"
    printf '%-28s %s\n' "${name}" "$([ -n "${VALUE[${name}]:-}" ] && echo set || echo empty)"
  done
  echo "file: ${FILE} ($(stat -c '%a %U' "${FILE}"))"
}

cmd_sync() {
  local restart=1
  [ "${1:-}" != "--no-restart" ] || restart=0
  [ -f "${FILE}" ] || { echo "${FILE} does not exist yet: run init" >&2; return 1; }
  [ "$(stat -c '%a' "${FILE}")" = "600" ] || { echo "${FILE} must be mode 600 (it is $(stat -c '%a' "${FILE}"))" >&2; return 1; }
  read_file
  local spec ns secret pairs workloads pair key name before after have missing args w
  declare -A bounce=()
  for spec in "${SECRETS[@]}"; do
    IFS='|' read -r ns secret pairs workloads <<<"${spec}"
    kubectl get ns "${ns}" >/dev/null 2>&1 || { echo "  skip  ${ns}/${secret}: the namespace does not exist yet"; continue; }
    IFS=',' read -ra list <<<"${pairs}"
    have=0; missing=(); args=()
    for pair in "${list[@]}"; do
      key="${pair%%=*}"; name="${pair#*=}"
      if [ -n "${VALUE[${name}]:-}" ]; then have=$((have + 1)); args+=("--from-literal=${key}=${VALUE[${name}]}"); else missing+=("${name}"); fi
    done
    if [ "${have}" -eq 0 ]; then echo "  skip  ${ns}/${secret}: ${missing[*]} not in the file"; continue; fi
    if [ "${#missing[@]}" -gt 0 ]; then echo "  WARN  ${ns}/${secret}: ${missing[*]} empty, so it was left as it is"; continue; fi
    before="$({ kubectl -n "${ns}" get secret "${secret}" -o jsonpath='{.data}' 2>/dev/null || true; } | sha256sum)"
    kubectl -n "${ns}" create secret generic "${secret}" "${args[@]}" --dry-run=client -o yaml \
      | kubectl apply -f - 2> >(grep -v 'last-applied-configuration' >&2) >/dev/null
    after="$(kubectl -n "${ns}" get secret "${secret}" -o jsonpath='{.data}' | sha256sum)"
    if [ "${before}" = "${after}" ]; then echo "  same  ${ns}/${secret}"
    else
      echo "  wrote ${ns}/${secret}"
      for w in ${workloads}; do bounce["${ns} ${w}"]=1; done
    fi
  done
  for w in "${!bounce[@]}"; do
    read -r ns name <<<"${w}"
    kubectl -n "${ns}" get "${name}" >/dev/null 2>&1 || continue
    if [ "${restart}" -eq 1 ]; then kubectl -n "${ns}" rollout restart "${name}" >/dev/null && echo "  restarted ${ns} ${name} (its secret changed)"
    else echo "  (${ns} ${name} will pick the change up at its next restart)"; fi
  done
}

case "${1:-}" in
  init) cmd_init ;;
  list) cmd_list ;;
  sync) shift; cmd_sync "${1:-}" ;;
  *) sed -n '2,24p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' ; exit 2 ;;
esac
