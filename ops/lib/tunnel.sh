#!/usr/bin/env bash
# Tunnel to Kubernetes services via SSM through an EKS node.
# Forwards directly to ClusterIPs, bypassing the ALB and its IP allowlist.
set -euo pipefail

# Pre-flight tool check — this script is runnable directly (not only via
# ./platformctl), so fail fast with a clear message instead of a cryptic
# "command not found" partway through.
for _tool in kubectl aws session-manager-plugin; do
  command -v "$_tool" >/dev/null 2>&1 || { echo "Error: missing required tool: ${_tool} (see README prerequisites)" >&2; exit 1; }
done

NAMESPACE="ai-platform"

CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' | awk -F/ '{print $NF}')
echo "→ Finding an EKS node in the criticaladdons node group (cluster: $CLUSTER_NAME)..."
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:eks:cluster-name,Values=$CLUSTER_NAME" \
            "Name=tag:eks:nodegroup-name,Values=*criticaladdons*" \
            "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text)
[ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ] && { echo "ERROR: No running EKS node found."; exit 1; }
echo "  Instance: $INSTANCE_ID"

echo ""
echo "→ Resolving Kubernetes service ClusterIPs..."
OPENWEBUI_IP=$(kubectl get svc open-webui -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
LITELLM_IP=$(kubectl get svc litellm -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
LANGFUSE_IP=$(kubectl get svc langfuse-web -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
# The dashboard's Approve/Dismiss mutations require the OIDC bearer token that
# oauth2-proxy injects (--set-authorization-header=true) before forwarding to
# cluster-dashboard:9090 — the same path the ALB ingress uses. Forwarding
# straight to the cluster-dashboard Service (port 9090) skips oauth2-proxy
# entirely: the read-only topology view still renders (no auth needed), but
# any mutation 401s with "missing bearer token" even though you're logged in,
# because the backend never receives the header. So we tunnel oauth2-proxy's
# port (4180) and map it to local :9090, keeping the same URL for the operator.
DASHBOARD_IP=$(kubectl get svc oauth2-proxy -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
DASHBOARD_REMOTE_PORT=$(kubectl get svc oauth2-proxy -n "$NAMESPACE" -o jsonpath='{.spec.ports[0].port}')
echo "  open-webui:        $OPENWEBUI_IP:8080"
echo "  litellm:           $LITELLM_IP:4000"
echo "  langfuse-web:      $LANGFUSE_IP:3000"
echo "  cluster-dashboard: $DASHBOARD_IP:$DASHBOARD_REMOTE_PORT (via oauth2-proxy, so Approve/Dismiss are authenticated)"

echo ""
echo "→ Starting SSM tunnels (via ClusterIP — bypasses ALB allowlist)..."
echo "  Open WebUI:  http://localhost:8080"
echo "  LiteLLM:     http://localhost:4000"
echo "  Langfuse:    http://localhost:3000"
echo "  Dashboard:   http://localhost:9090"
echo ""

# Each SSM port-forwarding session can go idle and exit on its own (default
# ~20min inactivity timeout) while the local port stays bound — curl/browsers
# then get instant, generic 500s that look like the *application* is broken,
# when the tunnel session is simply gone. A small watchdog polls each local
# port and relaunches only the ones that died, so a long dashboard session
# keeps working without the operator having to notice and restart everything.
# Each SSM port-forwarding session can go idle and exit on its own (default
# ~20min inactivity timeout) while the local port stays bound — curl/browsers
# then get instant, generic 500s that look like the *application* is broken,
# when the tunnel session is simply gone. A small watchdog polls each local
# port and relaunches only the ones that died, so a long dashboard session
# keeps working without the operator having to notice and restart everything.
# (Plain indexed arrays, not associative — this must run on bash 3.2, macOS's
# default /bin/bash, which has no `declare -A`.)
LOCAL_PORTS=(8080 4000 3000 9090)
REMOTE_HOSTS=("$OPENWEBUI_IP" "$LITELLM_IP" "$LANGFUSE_IP" "$DASHBOARD_IP")
REMOTE_PORTS=(8080 4000 3000 "$DASHBOARD_REMOTE_PORT")
SSM_PIDS=(0 0 0 0)

start_forward() {
  local i="$1"
  aws ssm start-session --target "$INSTANCE_ID" \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "{\"host\":[\"${REMOTE_HOSTS[$i]}\"],\"portNumber\":[\"${REMOTE_PORTS[$i]}\"],\"localPortNumber\":[\"${LOCAL_PORTS[$i]}\"]}" \
    >/dev/null 2>&1 &
  SSM_PIDS[$i]=$!
}

for i in 0 1 2 3; do start_forward "$i"; done

cleanup() { kill "${SSM_PIDS[@]}" 2>/dev/null; exit; }
trap cleanup INT TERM

while true; do
  sleep 30
  for i in 0 1 2 3; do
    if ! kill -0 "${SSM_PIDS[$i]}" 2>/dev/null; then
      echo "→ tunnel :${LOCAL_PORTS[$i]} died, restarting..." >&2
      start_forward "$i"
    fi
  done
done
