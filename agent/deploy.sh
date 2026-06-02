#!/usr/bin/env bash
# agent/deploy.sh
#
# Builds and deploys the Registry Governor ag_ui_adk container to the
# ping-devops-cprice namespace in the existing K8s cluster.
#
# Prerequisites:
#   - Docker logged in to docker.io/pricecs
#   - kubectl context pointing at the target cluster
#
# Usage:
#   ./agent/deploy.sh
#
# Optional env:
#   IMAGE   — override the image name (default: docker.io/pricecs/registry-governor:latest)
#   PUSH    — set to "false" to skip docker push (default: true)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-docker.io/pricecs/registry-governor:latest}"
PUSH="${PUSH:-true}"

echo "[deploy] Building image: ${IMAGE}"
docker build -t "${IMAGE}" "${SCRIPT_DIR}"

if [[ "${PUSH}" == "true" ]]; then
  echo "[deploy] Pushing image: ${IMAGE}"
  docker push "${IMAGE}"
fi

echo "[deploy] Applying k8s/registry-agent.yaml..."
kubectl apply -f "${SCRIPT_DIR}/../k8s/registry-agent.yaml"

echo "[deploy] Rolling out registry-agent..."
kubectl rollout restart deployment/registry-agent -n ping-devops-cprice
kubectl rollout status deployment/registry-agent -n ping-devops-cprice --timeout=120s

echo "[deploy] Done. Agent live at https://notflux-registry-agent.ping-devops.com"

