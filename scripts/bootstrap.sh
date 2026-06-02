#!/usr/bin/env bash
# scripts/bootstrap.sh
#
# Bootstraps the Agent Registry by applying all Kubernetes manifests to the
# 'security' namespace and seeding SpiceDB with the schema in schema/schema.zed.
#
# Usage:
#   ./scripts/bootstrap.sh [--patch-http-connector] [--context <kube-context>]
#
# Options:
#   --patch-http-connector   Also apply the HTTP Data Connector patch (patch-p1az.yaml)
#   --context <name>         Use the specified kubectl context (default: current context)
#
# Prerequisites:
#   - kubectl installed and configured
#   - The preshared key in k8s/secrets.yaml has been replaced with a real value
#
set -euo pipefail

###############################################################################
# Helpers
###############################################################################
log()  { echo "[bootstrap] $*"; }
warn() { echo "[bootstrap] WARNING: $*" >&2; }
die()  { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

###############################################################################
# Defaults
###############################################################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
K8S_DIR="${REPO_ROOT}/k8s"
SCHEMA_DIR="${REPO_ROOT}/schema"

NAMESPACE="ping-devops-cprice"
APPLY_HTTP_CONNECTOR=false
KUBECTL_CONTEXT=""

###############################################################################
# Argument parsing
###############################################################################
while [[ $# -gt 0 ]]; do
  case "$1" in
    --patch-http-connector)
      APPLY_HTTP_CONNECTOR=true
      shift
      ;;
    --context)
      [[ -n "${2:-}" ]] || die "--context requires a value"
      KUBECTL_CONTEXT="$2"
      shift 2
      ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

###############################################################################
# Build kubectl base command
###############################################################################
KUBECTL="kubectl"
if [[ -n "${KUBECTL_CONTEXT}" ]]; then
  KUBECTL="${KUBECTL} --context=${KUBECTL_CONTEXT}"
fi

###############################################################################
# Preflight checks
###############################################################################
command -v kubectl &>/dev/null || die "kubectl not found in PATH"

log "Using kubectl context: $(${KUBECTL} config current-context 2>/dev/null || echo 'unknown')"
log "Target namespace: ${NAMESPACE}"

# Warn if the preshared key still contains the placeholder value.
if grep -q "REPLACE_ME" "${K8S_DIR}/secrets.yaml"; then
  warn "k8s/secrets.yaml still contains the placeholder preshared key."
  warn "Replace it with a real value before deploying to a shared environment."
  read -rp "[bootstrap] Continue anyway? [y/N] " confirm
  [[ "${confirm,,}" == "y" ]] || die "Aborted by user."
fi

###############################################################################
# Apply manifests
###############################################################################
log "Applying secrets..."
${KUBECTL} apply -f "${K8S_DIR}/secrets.yaml"

log "Applying deployment (Namespace + Deployment + Service + Ingress)..."
${KUBECTL} apply -f "${K8S_DIR}/deployment.yaml"

###############################################################################
# Wait for SpiceDB rollout before applying schema
###############################################################################
log "Waiting for SpiceDB rollout to complete..."
${KUBECTL} rollout status deployment/spicedb \
  --namespace "${NAMESPACE}" \
  --timeout=120s \
  || die "SpiceDB deployment did not become ready within 120s."

###############################################################################
# Apply schema via zed CLI container image
# Creates an ephemeral Job using the authzed/zed image. The schema file is
# mounted via a transient ConfigMap; the preshared key is injected from the
# existing Secret (never written to the job spec in plaintext).
###############################################################################
log "Creating schema ConfigMap from ${SCHEMA_DIR}/schema.zed ..."
${KUBECTL} create configmap spicedb-schema \
  --from-file=schema.zed="${SCHEMA_DIR}/schema.zed" \
  --namespace "${NAMESPACE}" \
  --dry-run=client -o yaml | ${KUBECTL} apply -f -

log "Submitting schema-apply Job (authzed/zed)..."
${KUBECTL} apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: spicedb-schema-apply
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: spicedb
    app.kubernetes.io/component: schema
    app.kubernetes.io/part-of: notflux-registry
spec:
  # Auto-delete 60 s after the Job finishes so it doesn't litter the namespace.
  ttlSecondsAfterFinished: 60
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: zed
          image: authzed/zed:latest
          imagePullPolicy: IfNotPresent
          env:
            - name: ZED_TOKEN
              valueFrom:
                secretKeyRef:
                  name: spicedb-preshared-key
                  key: presharedKey
          # Kubernetes expands \$(ZED_TOKEN) from the env entry above at pod start.
          command:
            - zed
            - --endpoint
            - spicedb.ping-devops-cprice.svc.cluster.local:50051
            - --token
            - \$(ZED_TOKEN)
            - --insecure
            - schema
            - write
            - /schema/schema.zed
          volumeMounts:
            - name: schema
              mountPath: /schema
              readOnly: true
      volumes:
        - name: schema
          configMap:
            name: spicedb-schema
EOF

log "Waiting for schema Job to complete..."
${KUBECTL} wait job/spicedb-schema-apply \
  --namespace "${NAMESPACE}" \
  --for=condition=complete \
  --timeout=60s \
  || die "Schema apply Job did not complete. Inspect with: kubectl logs -n ${NAMESPACE} -l job-name=spicedb-schema-apply"

log "Cleaning up schema Job and ConfigMap..."
${KUBECTL} delete job spicedb-schema-apply \
  --namespace "${NAMESPACE}" \
  --ignore-not-found
${KUBECTL} delete configmap spicedb-schema \
  --namespace "${NAMESPACE}" \
  --ignore-not-found

log "Schema applied successfully."

###############################################################################
# Optional: HTTP Data Connector (Envoy sidecar) patch
###############################################################################
if [[ "${APPLY_HTTP_CONNECTOR}" == "true" ]]; then
  log "Applying HTTP Data Connector ConfigMap and patch..."
  ${KUBECTL} apply -f "${K8S_DIR}/patch-p1az.yaml"
  log "Patching SpiceDB Deployment with HTTP Data Connector sidecar..."
  ${KUBECTL} patch deployment spicedb \
    --namespace "${NAMESPACE}" \
    --patch-file "${K8S_DIR}/patch-p1az.yaml"
fi

log "Bootstrap complete."

###############################################################################
# Helpers
###############################################################################
log()  { echo "[bootstrap] $*"; }
warn() { echo "[bootstrap] WARNING: $*" >&2; }
die()  { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

###############################################################################
# Defaults
###############################################################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
K8S_DIR="${REPO_ROOT}/k8s"

APPLY_HTTP_CONNECTOR=false
KUBECTL_CONTEXT=""

###############################################################################
# Argument parsing
###############################################################################
while [[ $# -gt 0 ]]; do
  case "$1" in
    --patch-http-connector)
      APPLY_HTTP_CONNECTOR=true
      shift
      ;;
    --context)
      [[ -n "${2:-}" ]] || die "--context requires a value"
      KUBECTL_CONTEXT="$2"
      shift 2
      ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

###############################################################################
# Build kubectl base command
###############################################################################
KUBECTL="kubectl"
if [[ -n "${KUBECTL_CONTEXT}" ]]; then
  KUBECTL="${KUBECTL} --context=${KUBECTL_CONTEXT}"
fi

###############################################################################
# Preflight checks
###############################################################################
command -v kubectl &>/dev/null || die "kubectl not found in PATH"

log "Using kubectl context: $(${KUBECTL} config current-context 2>/dev/null || echo 'unknown')"

# Warn if the preshared key still contains the placeholder value.
if grep -q "REPLACE_ME" "${K8S_DIR}/secrets.yaml"; then
  warn "k8s/secrets.yaml still contains the placeholder preshared key."
  warn "Replace it with a real value before deploying to a shared environment."
  read -rp "[bootstrap] Continue anyway? [y/N] " confirm
  [[ "${confirm,,}" == "y" ]] || die "Aborted by user."
fi

###############################################################################
# Apply manifests
###############################################################################
log "Applying secrets..."
${KUBECTL} apply -f "${K8S_DIR}/secrets.yaml"

log "Applying deployment (Namespace + Deployment + Service)..."
${KUBECTL} apply -f "${K8S_DIR}/deployment.yaml"

if [[ "${APPLY_HTTP_CONNECTOR}" == "true" ]]; then
  log "Applying HTTP Data Connector patch..."
  ${KUBECTL} apply -f "${K8S_DIR}/patch-p1az.yaml"
  log "Patching SpiceDB Deployment with HTTP Data Connector sidecar..."
  ${KUBECTL} patch deployment spicedb \
    --namespace agent-registry \
    --patch-file "${K8S_DIR}/patch-p1az.yaml"
fi

###############################################################################
# Wait for rollout
###############################################################################
log "Waiting for SpiceDB rollout to complete..."
${KUBECTL} rollout status deployment/spicedb \
  --namespace agent-registry \
  --timeout=120s

###############################################################################
# Summary
###############################################################################
log "Agent Registry is up."
log ""
log "  gRPC  endpoint: $(${KUBECTL} get svc spicedb -n agent-registry -o jsonpath='{.spec.clusterIP}'):50051"
log "  HTTP  endpoint: $(${KUBECTL} get svc spicedb -n agent-registry -o jsonpath='{.spec.clusterIP}'):8443"
if [[ "${APPLY_HTTP_CONNECTOR}" == "true" ]]; then
  log "  HTTP connector: $(${KUBECTL} get svc spicedb -n agent-registry -o jsonpath='{.spec.clusterIP}'):8080"
fi
log ""
log "Next step: load the schema"
log "  zed schema write schema/schema.zed \\"
log "    --endpoint=<CLUSTER_IP>:50051 \\"
log "    --token=<preshared-key>"
