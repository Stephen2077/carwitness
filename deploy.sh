#!/usr/bin/env bash
# CarWitness 车证 — deploy to the team Kubernetes namespace (no Docker build).
#
#   ./deploy.sh          create/update ConfigMap + Secret + Deployment/Service/Ingress, restart, verify
#   ./deploy.sh logs     tail the app logs
#   ./deploy.sh status   show pods / service / ingress
#
# Run from the repo root on the team VM. Reads team config from /config/*.config
# (KEY=value lines; parsed with grep/cut, never sourced) and never prints secret values.
set -euo pipefail

APP_NAME=carwitness
CM_NAME=carwitness-code
SECRET_NAME=carwitness-creds
IMAGE=python:3.12-slim
PORT=8080
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/app"

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

# ---------------------------------------------------------------- team config
CONFIG_FILE="${CONFIG_FILE:-}"
if [[ -z "$CONFIG_FILE" ]]; then
  shopt -s nullglob
  cfgs=(/config/*.config)
  shopt -u nullglob
  [[ ${#cfgs[@]} -gt 0 ]] || die "no team config found at /config/*.config (set CONFIG_FILE=/path/to/team.config)"
  CONFIG_FILE="${cfgs[0]}"
fi
[[ -r "$CONFIG_FILE" ]] || die "cannot read $CONFIG_FILE"

# cfg KEY -> value from the config file (first match; strips CR and surrounding quotes).
cfg() {
  local v
  v="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$CONFIG_FILE" | head -n1 | cut -d= -f2- || true)"
  v="${v%$'\r'}"
  v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
  printf '%s' "$v"
}
# env_or_cfg KEY -> shell environment first, then the config file.
env_or_cfg() {
  local v="${!1:-}"
  [[ -n "$v" ]] && { printf '%s' "$v"; return; }
  cfg "$1"
}

# USERNAME comes from the file only: the login shell may already define $USERNAME as the OS user.
TEAM_USER="$(cfg USERNAME)"
[[ -n "$TEAM_USER" ]] || die "USERNAME not found in $CONFIG_FILE"
NS="$TEAM_USER"
TEAM_NUM="$(printf '%s' "$TEAM_USER" | grep -oE '[0-9]+' | tail -n1 || true)"
[[ -n "$TEAM_NUM" ]] || die "could not get team number from USERNAME=$TEAM_USER"
APP_HOST="${APP_HOST:-video-lab-team-${TEAM_NUM}.cosmos.vastdata.com}"

# ---------------------------------------------------------------- kubeconfig
if [[ -z "${KUBECONFIG:-}" || ! -r "${KUBECONFIG%%:*}" ]]; then
  if [[ -r "/config/${TEAM_USER}-k8s.yaml" ]]; then
    export KUBECONFIG="/config/${TEAM_USER}-k8s.yaml"
  elif [[ -r /config/kubeconfig ]]; then
    export KUBECONFIG=/config/kubeconfig
  else
    die "no kubeconfig at /config/${TEAM_USER}-k8s.yaml or /config/kubeconfig"
  fi
fi
command -v kubectl >/dev/null || die "kubectl not found"
K=(kubectl -n "$NS")

# ---------------------------------------------------------------- subcommands
case "${1:-deploy}" in
  logs)
    exec "${K[@]}" logs -f "deploy/$APP_NAME" --tail=200 ;;
  status)
    "${K[@]}" get pods -l "app=$APP_NAME" -o wide
    "${K[@]}" get svc,ingress -l "app=$APP_NAME"
    exit 0 ;;
  deploy) ;;
  *) die "usage: $0 [deploy|logs|status]" ;;
esac

info "Config: $CONFIG_FILE | namespace: $NS | host: $APP_HOST"
info "kubeconfig: $KUBECONFIG"

# ---------------------------------------------------------------- credentials
VSS_URL="$(cfg INGRESS_URL)"
VSS_PASSWORD="$(cfg PASSWORD)"
WANDB_API_KEY="$(env_or_cfg WANDB_API_KEY)"
WANDB_TEAM="$(env_or_cfg WANDB_TEAM)"
WANDB_PROJECT="$(env_or_cfg WANDB_PROJECT)"
LLM_MODEL="${LLM_MODEL:-nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B}"

[[ -n "$VSS_URL" ]] || die "INGRESS_URL not found in $CONFIG_FILE"
[[ -n "$VSS_PASSWORD" ]] || die "PASSWORD not found in $CONFIG_FILE"
[[ -n "$WANDB_API_KEY" ]] || die "WANDB_API_KEY not found in the environment or in $CONFIG_FILE.
       Export it first:  export WANDB_API_KEY=...   (get it from https://wandb.ai/authorize)"
[[ "$VSS_URL" =~ ^https?:// ]] || VSS_URL="https://$VSS_URL"
VSS_URL="${VSS_URL%/}"
# INGRESS_URL only resolves on the VM, not inside pods: prefer the in-cluster backend Service.
BACKEND_PORT="$("${K[@]}" get svc video-backend-service -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
if [[ -n "$BACKEND_PORT" ]]; then
  VSS_URL="http://video-backend-service.${NS}.svc.cluster.local:${BACKEND_PORT}"
  info "VSS backend (in-cluster): $VSS_URL"
fi
[[ -n "$WANDB_TEAM" && -n "$WANDB_PROJECT" ]] || echo "WARN: WANDB_TEAM/WANDB_PROJECT not set; inference will run without a project header and Weave tracing is off"

# ---------------------------------------------------------------- ConfigMap (code)
[[ -f "$APP_DIR/main.py" && -f "$APP_DIR/index.html" ]] || die "expected $APP_DIR/main.py and $APP_DIR/index.html"
CM_ARGS=(--from-file="main.py=$APP_DIR/main.py" --from-file="index.html=$APP_DIR/index.html")
CODE_FILES=("$APP_DIR/main.py" "$APP_DIR/index.html")
if [[ -f "$APP_DIR/requirements.txt" ]]; then
  CM_ARGS+=(--from-file="requirements.txt=$APP_DIR/requirements.txt")
  CODE_FILES+=("$APP_DIR/requirements.txt")
fi
CODE_BYTES="$(cat "${CODE_FILES[@]}" | wc -c | tr -d ' ')"
[[ "$CODE_BYTES" -lt 1000000 ]] || die "app code is ${CODE_BYTES} bytes; ConfigMaps must stay under ~1 MiB"
info "ConfigMap $CM_NAME (${#CODE_FILES[@]} files, ${CODE_BYTES} bytes)"
"${K[@]}" create configmap "$CM_NAME" "${CM_ARGS[@]}" --dry-run=client -o yaml | "${K[@]}" apply -f -

# ---------------------------------------------------------------- Secret (credentials)
# Written to a private temp env-file so values never appear in argv or output.
ENV_FILE="$(umask 077; mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
{
  printf 'VSS_URL=%s\n' "$VSS_URL"
  printf 'VSS_USERNAME=%s\n' "$TEAM_USER"
  printf 'VSS_PASSWORD=%s\n' "$VSS_PASSWORD"
  printf 'WANDB_API_KEY=%s\n' "$WANDB_API_KEY"
  printf 'WANDB_TEAM=%s\n' "$WANDB_TEAM"
  printf 'WANDB_PROJECT=%s\n' "$WANDB_PROJECT"
  if [[ -n "$LLM_MODEL" ]]; then printf 'LLM_MODEL=%s\n' "$LLM_MODEL"; fi
} > "$ENV_FILE"
info "Secret $SECRET_NAME (VSS_URL, VSS_USERNAME, VSS_PASSWORD, WANDB_*${LLM_MODEL:+, LLM_MODEL})"
"${K[@]}" create secret generic "$SECRET_NAME" --from-env-file="$ENV_FILE" --dry-run=client -o yaml \
  | "${K[@]}" apply -f - >/dev/null
echo "secret/$SECRET_NAME configured"

# ---------------------------------------------------------------- Deployment / Service / Ingress
info "Deployment, Service, Ingress"
"${K[@]}" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  replicas: 1
  selector:
    matchLabels: {app: ${APP_NAME}}
  template:
    metadata:
      labels: {app: ${APP_NAME}}
    spec:
      containers:
        - name: ${APP_NAME}
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          workingDir: /app
          command: ["sh", "-c"]
          args:
            - |
              if [ -f requirements.txt ]; then pip install --no-cache-dir -q -r requirements.txt; fi
              # Optional W&B Weave tracing; the app runs without it if this fails.
              pip install --no-cache-dir -q weave || echo "weave not installed; tracing off"
              exec python main.py
          ports:
            - containerPort: ${PORT}
          env:
            - {name: PORT, value: "${PORT}"}
            - {name: PYTHONUNBUFFERED, value: "1"}
          envFrom:
            - secretRef: {name: ${SECRET_NAME}}
          volumeMounts:
            - {name: code, mountPath: /app, readOnly: true}
          readinessProbe:
            httpGet: {path: /health, port: ${PORT}}
            initialDelaySeconds: 3
            periodSeconds: 5
          livenessProbe:
            httpGet: {path: /health, port: ${PORT}}
            initialDelaySeconds: 300
            periodSeconds: 20
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {memory: 1Gi}
      volumes:
        - name: code
          configMap: {name: ${CM_NAME}}
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  selector: {app: ${APP_NAME}}
  ports:
    - {name: http, port: 80, targetPort: ${PORT}}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /\$2
    nginx.ingress.kubernetes.io/use-regex: "true"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "300"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
spec:
  ingressClassName: nginx
  rules:
    - host: ${APP_HOST}
      http:
        paths:
          - path: /app(/|\$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: ${APP_NAME}
                port: {number: 80}
EOF

# ---------------------------------------------------------------- roll out + verify
info "Restarting so the pod picks up the new ConfigMap/Secret"
"${K[@]}" rollout restart "deploy/$APP_NAME"
if ! "${K[@]}" rollout status "deploy/$APP_NAME" --timeout=300s; then
  echo "Rollout did not finish. Recent pod state and logs:" >&2
  "${K[@]}" get pods -l "app=$APP_NAME" -o wide >&2 || true
  "${K[@]}" logs "deploy/$APP_NAME" --tail=50 >&2 || true
  exit 1
fi
"${K[@]}" get pods -l "app=$APP_NAME" -o wide

info "Health check: http://$APP_HOST/app/health"
if command -v curl >/dev/null; then
  ok=""
  for _ in 1 2 3 4 5 6; do
    if curl -fsS --max-time 10 "http://$APP_HOST/app/health"; then ok=1; echo; break; fi
    sleep 5
  done
  [[ -n "$ok" ]] || echo "WARN: health check via Ingress failed (pod is Ready; the Ingress may need a minute). Try: ./deploy.sh logs"
else
  echo "WARN: curl not found; skipping health check"
fi

cat <<MSG

Deployed CarWitness to namespace $NS.
  -> Open https://workshop.thecosmoslabs.com and click **App**
     (direct: http://$APP_HOST/app/)
  Logs: ./deploy.sh logs
MSG
