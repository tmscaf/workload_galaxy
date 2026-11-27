#!/usr/bin/env bash
# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

# Build one part of the suite and put it on a k3s cluster that has no image registry: the image is
# built here, shipped to the node as a tarball, imported, and the part's manifests are applied.
#
#   tools/deploy-k3s.sh <kubernetes|network|esxi|shell> [image-tag]
#
# Nothing about a particular cluster is in this repository. It comes from two private places:
#
# 1. A settings file, ${GALAXY_DEPLOY_CONFIG:-~/.config/workload-galaxy/deploy.env}:
#
#      K3S_SSH=my-node                   # ssh name of the k3s node (it must run kubectl and k3s)
#      KUBECONFIG=~/.kube/my-cluster     # how kubectl reaches the cluster from here
#      OVERLAYS=~/somewhere/overlays     # a folder with one overlay per part (see below)
#      URL_SHELL=http://my-node:8089     # optional, per part: printed when the deploy is done
#
# 2. An overlay per part, ${OVERLAYS}/<part>/kustomization.yaml: the kustomize overlay that says how
#    the part runs on your cluster (namespace, how it is exposed, any settings it should start
#    with). It refers to the part's base manifests as ../k8s. docs/deploying.md has a complete one.
#
# Secrets are not passed here either: the cluster's private secrets file is synced into Kubernetes
# Secrets first (tools/cluster-secrets.sh).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PART="${1:-}"
case "${PART}" in
  kubernetes) DIR=modules/kubernetes; NS=workload-galaxy;       IMAGE_NAME=workload-galaxy;       PROJECT="Workload Galaxy";       ARG=GALAXY;     WORKLOADS="deploy/workload-galaxy ds/galaxy-agent" ;;
  network)    DIR=modules/network;    NS=network-galaxy;        IMAGE_NAME=network-galaxy;        PROJECT="Network Galaxy";        ARG=NETGALAXY;  WORKLOADS="deploy/network-galaxy" ;;
  esxi)       DIR=modules/esxi;       NS=esxi-galaxy;           IMAGE_NAME=esxi-galaxy;           PROJECT="ESXi Galaxy";           ARG=ESXIGALAXY; WORKLOADS="deploy/esxi-galaxy" ;;
  shell)      DIR=shell;              NS=workload-galaxy-shell; IMAGE_NAME=workload-galaxy-shell; PROJECT="Workload Galaxy Shell"; ARG=GALAXY;     WORKLOADS="deploy/workload-galaxy-shell" ;;
  *) sed -n '5,24p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

CONFIG="${GALAXY_DEPLOY_CONFIG:-$HOME/.config/workload-galaxy/deploy.env}"
[ -f "${CONFIG}" ] || { echo "!! ${CONFIG} not found. It says which cluster to deploy to; see the top of $0."; exit 1; }
set -a
# shellcheck disable=SC1090
. "${CONFIG}"
set +a
: "${K3S_SSH:?set K3S_SSH in ${CONFIG} (the ssh name of the k3s node)}"
: "${OVERLAYS:?set OVERLAYS in ${CONFIG} (the folder holding your overlays)}"
OVERLAYS="${OVERLAYS/#\~/$HOME}"
[ -z "${KUBECONFIG:-}" ] || export KUBECONFIG="${KUBECONFIG/#\~/$HOME}"
OVERLAY="${OVERLAYS}/${PART}"
[ -f "${OVERLAY}/kustomization.yaml" ] || { echo "!! no overlay at ${OVERLAY}/kustomization.yaml (see docs/deploying.md)"; exit 1; }

VERSION="$(tr -d ' \n' < "${REPO}/${DIR}/VERSION")"
BUILD="$(date -u +%Y%m%d.%H%M)"
TAG="${2:-${VERSION}-${BUILD}}"
IMAGE="${IMAGE_NAME}:${TAG}"
SSH_OPTS=(-o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
retry() {      # every remote step retries for about two minutes: long enough to ride out a dropped link
  local n=0
  until "$@"; do
    n=$((n + 1)); [ "${n}" -ge 12 ] && return 1
    echo "    link down? retrying in 10s (${n}/11)"; sleep 10
  done
}

if ! kubectl get ns "${NS}" >/dev/null 2>&1; then
  if kubectl api-resources --api-group=management.cattle.io -o name 2>/dev/null | grep -q '^projects\.'; then
    echo "==> creating Rancher project '${PROJECT}' and namespace ${NS}"
    PID=$(kubectl create -o name -f - <<YAML | cut -d/ -f2
apiVersion: management.cattle.io/v3
kind: Project
metadata:
  generateName: p-
  namespace: local
spec:
  clusterName: local
  displayName: "${PROJECT}"
YAML
)
    kubectl create ns "${NS}"
    kubectl annotate ns "${NS}" field.cattle.io/projectId="local:${PID}" --overwrite
  else
    echo "==> creating namespace ${NS}"
    kubectl create ns "${NS}"
  fi
fi

echo "==> syncing secrets from the cluster's private file"
ssh "${SSH_OPTS[@]}" "${K3S_SSH}" 'bash -s -- sync --no-restart' < "${REPO}/tools/cluster-secrets.sh" \
  || { echo "!! no secrets file on the cluster yet. Create it once with:"; echo "     ssh ${K3S_SSH} 'bash -s -- init' < tools/cluster-secrets.sh"; exit 1; }
if [ "${PART}" = "network" ] && ! kubectl -n "${NS}" get secret network-galaxy-ntopng >/dev/null 2>&1; then
  echo "!! NTOPNG_USER / NTOPNG_PASSWORD are empty in the cluster's secrets file: fill them in and run this again"
  exit 1
fi

echo "==> building ${IMAGE} (linux/amd64)"
# The build context is the repository root: every image includes core/py as well as its own part.
docker buildx build --platform linux/amd64 -t "${IMAGE}" --load -f "${REPO}/${DIR}/Dockerfile" \
  --build-arg "${ARG}_VERSION=${VERSION}" --build-arg "${ARG}_BUILD=${BUILD}" "${REPO}"

echo "==> shipping image to ${K3S_SSH}"
# A single large copy can saturate a VPN link until it resets: send throttled chunks, with retries,
# and check the whole thing arrived intact.
WORK="$(mktemp -d -t galaxy-deploy)"
trap 'rm -rf "${WORK}"' EXIT
TARBALL="${WORK}/image.tar.gz"
docker save "${IMAGE}" | gzip > "${TARBALL}"
SUM="$(shasum -a 256 "${TARBALL}" | cut -d' ' -f1)"
split -b 4m "${TARBALL}" "${WORK}/part."
REMOTE="/tmp/galaxy-${PART}"
retry ssh "${SSH_OPTS[@]}" "${K3S_SSH}" "rm -rf ${REMOTE}-parts ${REMOTE}.tar.gz && mkdir -p ${REMOTE}-parts"
for part in "${WORK}"/part.*; do
  retry scp -q -l 12000 "${SSH_OPTS[@]}" "${part}" "${K3S_SSH}:${REMOTE}-parts/" \
    || { echo "!! could not upload $(basename "${part}")"; exit 1; }
  printf '.'
done
echo
retry ssh "${SSH_OPTS[@]}" "${K3S_SSH}" "cat ${REMOTE}-parts/part.* > ${REMOTE}.tar.gz \
  && echo '${SUM}  ${REMOTE}.tar.gz' | sha256sum -c --quiet"
retry ssh "${SSH_OPTS[@]}" "${K3S_SSH}" "sudo k3s ctr images import ${REMOTE}.tar.gz >/dev/null \
  && rm -rf ${REMOTE}.tar.gz ${REMOTE}-parts"

echo "==> applying manifests"
# The overlay is private and stays as it is: a copy is stamped with this build and applied.
mkdir -p "${WORK}/apply"
cp -R "${REPO}/${DIR}/deploy/k8s" "${WORK}/apply/k8s"
cp -R "${OVERLAY}" "${WORK}/apply/overlay"
STAMPED="${WORK}/apply/overlay/kustomization.yaml"
sed -i.bak -E -e "s/^(    newTag: ).*/\1${TAG}/" \
  -e "s/^(      app.kubernetes.io\/version: ).*/\1\"${VERSION}\"/" \
  -e "s/^(  ${IMAGE_NAME}\/build: ).*/\1\"${BUILD}\"/" "${STAMPED}"
rm -f "${STAMPED}.bak"
grep -q "newTag: ${TAG}" "${STAMPED}" || { echo "!! ${OVERLAY}/kustomization.yaml has no 'images: ... newTag:' line to stamp (see docs/deploying.md)"; exit 1; }
retry kubectl apply -k "${WORK}/apply/overlay"
for w in ${WORKLOADS}; do
  retry kubectl -n "${NS}" rollout status "${w}" --timeout=180s
done
if [ "${PART}" = "network" ]; then
  # The router sends syslog from one fixed UDP port, so the node keeps forwarding it to the old pod
  # until its connection entry is cleared (needs the conntrack package on the node).
  ssh "${SSH_OPTS[@]}" "${K3S_SSH}" 'sudo conntrack -D -p udp --dport 5514 >/dev/null 2>&1 || true'
fi

URL_VAR="URL_$(echo "${PART}" | tr '[:lower:]' '[:upper:]')"
echo "==> done: ${IMAGE_NAME} ${VERSION} (build ${BUILD})${!URL_VAR:+ -> ${!URL_VAR}}"
