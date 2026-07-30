#!/usr/bin/env python3
"""Cluster dashboard backend.

Two responsibilities:

1. (Original) Poll the Kubernetes API every 2s, build a JSON snapshot of
   nodes/pods/serving endpoints, and serve it at /data.json plus static
   HTML. Browser polls /data.json — no streaming, no proxying, no auth
   in browser.

2. (New) Surface Platform Health Agent approvals from the `platform_health_agent` postgres
   database. The agent now ships as a component of THIS dashboard app (its
   event-watcher + RBAC + config live alongside these manifests, all in the
   ai-platform namespace):
     - GET  /investigations           → list of pending investigations
     - POST /investigations/<id>/approve → spawn Remediator Job in
                                           the ai-platform namespace
     - POST /investigations/<id>/dismiss → mark dismissed (no Job)
   The /data.json payload also includes `approvals_pending` (count) and
   `approvals_available` (boolean) so the topbar can render a badge.

Backwards-compatibility: when the platform_health_agent DB is unreachable (e.g. the
agent is not deployed), all approvals endpoints return 503 and the
snapshot reports `approvals_available: false` — the existing dashboard
keeps working unchanged.
"""

from __future__ import annotations

import http.client
import http.server
import decimal
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import uuid
from urllib.parse import urlparse


def _json_default(o):
    """psycopg returns NUMERIC columns as Decimal, which json can't serialize."""
    if isinstance(o, decimal.Decimal):
        return float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

# psycopg is optional from the code's POV — if it can't import (image
# missing the dep), approvals are disabled rather than crashing the
# dashboard.
try:
    import psycopg                       # type: ignore[import-not-found]
    HAVE_PSYCOPG = True
except Exception:
    HAVE_PSYCOPG = False

try:
    import boto3                          # type: ignore[import-not-found]
    HAVE_BOTO3 = True
except Exception:
    HAVE_BOTO3 = False

# PyJWT (with cryptography) verifies the OIDC token that gates the mutating
# endpoints. Optional import: if it's missing AND auth is required, mutations
# fail closed (see authorize_mutation) rather than silently running unauthenticated.
try:
    import jwt                            # type: ignore[import-not-found]
    from jwt import PyJWKClient           # type: ignore[import-not-found]
    HAVE_JWT = True
except Exception:
    HAVE_JWT = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8080
HTML_DIR = "/html"
POLL_INTERVAL = 2

K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = int(os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

DB_HOST = os.environ.get("DB_HOST", "platform-db.ai-platform.svc.cluster.local")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "platform_health_agent")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")

PLATFORM_HEALTH_AGENT_NAMESPACE = os.environ.get("PLATFORM_HEALTH_AGENT_NAMESPACE", "ai-platform")
APPROVAL_EXPIRY_HOURS = int(os.environ.get("APPROVAL_EXPIRY_HOURS", "24"))
# An investigation Job has a 10-min hard deadline (activeDeadlineSeconds=600). If a
# row is still 'running'/'remediating' well past that, the Job died without writing
# its result and the row is stuck. Past this age it becomes dismissable from the
# History tab so users can clear the noise. Must comfortably exceed the Job deadline.
STALE_INVESTIGATION_MINUTES = int(os.environ.get("STALE_INVESTIGATION_MINUTES", "15"))
MAX_REMEDIATIONS_PER_DAY = int(os.environ.get("MAX_REMEDIATIONS_PER_DAY", "20"))
KIRO_MODEL_REMEDIATE = os.environ.get("KIRO_MODEL_REMEDIATE", "auto")
PYTHON_IMAGE = os.environ.get("PYTHON_IMAGE", "python:3.12-slim")
KUBECTL_VERSION = os.environ.get("KUBECTL_VERSION", "v1.32.5")

# ---------------------------------------------------------------------------
# Authentication / authorization for MUTATING endpoints
# ---------------------------------------------------------------------------
# The mutating endpoints (approve / dismiss / delete an investigation) spawn a
# cluster-mutating Remediator Job or destroy audit history, so they MUST be
# gated by a verified admin identity — never by network position.
#
# Why not trust oauth2-proxy's forwarded headers? Because the dashboard's own
# `platformctl tunnel` forwards straight to this pod's ClusterIP, BYPASSING
# oauth2-proxy — so any X-Forwarded-* / X-Auth-Request-* header is attacker-
# settable on that path. Instead the backend cryptographically verifies the
# OIDC ID token that oauth2-proxy injects as `Authorization: Bearer <jwt>`
# (--set-authorization-header) against the Cognito JWKS. That check is
# path-independent: a tunnel client with no valid, correctly-signed, in-group
# token is refused, and no header can forge a Cognito signature.
#
# Config (from the Terraform-managed oauth2-proxy-secrets + deployment env):
#   OIDC_ISSUER_URL   Cognito user-pool issuer (…/cognito-idp/<region>/<pool>)
#   OIDC_CLIENT_ID    app client id = expected `aud` of the ID token
#   OIDC_ALLOWED_GROUPS  comma-separated cognito:groups permitted to MUTATE
#                        (default: admins only — stricter than the view, which
#                         oauth2-proxy also opens to developers)
#   DASHBOARD_AUTH_REQUIRED  "true" (default) = fail closed when a request is
#                        unauthenticated OR auth is not configured. Set "false"
#                        ONLY on a trusted network with SSO disabled — mutations
#                        then run unauthenticated (logged loudly).
OIDC_ISSUER_URL = os.environ.get("OIDC_ISSUER_URL", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_ALLOWED_GROUPS = [
    g.strip() for g in os.environ.get("OIDC_ALLOWED_GROUPS", "ai-platform-admins").split(",") if g.strip()
]
DASHBOARD_AUTH_REQUIRED = os.environ.get("DASHBOARD_AUTH_REQUIRED", "true").lower() not in ("false", "0", "no")

_AUTH_CONFIGURED = bool(OIDC_ISSUER_URL and OIDC_CLIENT_ID and HAVE_JWT)
_jwks_client = None
_jwks_lock = threading.Lock()


def _get_jwks_client():
    """Lazily build (and cache) the Cognito JWKS client. Thread-safe."""
    global _jwks_client
    if _jwks_client is None:
        with _jwks_lock:
            if _jwks_client is None:
                _jwks_client = PyJWKClient(f"{OIDC_ISSUER_URL}/.well-known/jwks.json")
    return _jwks_client


def verify_identity(auth_header: str) -> tuple[str | None, str]:
    """Verify a Bearer OIDC token and authorize the caller for mutations.

    Returns (identity, "") on success, or (None, reason) on failure. `identity`
    is the verified email / username / sub — safe to record as the approver.
    """
    if not auth_header:
        return None, "missing bearer token"
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None, "malformed Authorization header"
    token = parts[1].strip()
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=OIDC_CLIENT_ID,
            issuer=OIDC_ISSUER_URL,
            options={"require": ["exp", "iss", "aud"]},
        )
    except Exception as e:  # signature/exp/iss/aud failure — deny
        return None, f"token verification failed: {type(e).__name__}"
    groups = claims.get("cognito:groups") or []
    if not any(g in OIDC_ALLOWED_GROUPS for g in groups):
        return None, "caller not in an allowed admin group"
    identity = claims.get("email") or claims.get("cognito:username") or claims.get("sub") or "unknown"
    return identity, ""

# Quick links — centralised entry points to the platform's web UIs, surfaced in
# the dashboard so users don't have to hunt for per-cluster hostnames.
#
# All of LiteLLM / Open WebUI / Langfuse / the dashboard share one internet-
# facing ALB (the `ai-platform` ingress group), differing only by port — so we
# discover the ALB hostname at runtime from the ingress status and append the
# known ports. ArgoCD is an EKS-managed capability with its own endpoint, so its
# URL can't be derived from the ALB; Terraform passes it in via ARGOCD_URL.
ARGOCD_URL = os.environ.get("ARGOCD_URL", "")
# Namespace + ingress name the ALB-fronted services live behind.
LINKS_INGRESS_NAMESPACE = os.environ.get("LINKS_INGRESS_NAMESPACE", "ai-platform")
LINKS_INGRESS_NAME = os.environ.get("LINKS_INGRESS_NAME", "ai-platform-litellm")


# ---------------------------------------------------------------------------
# Kubernetes API client (existing logic, preserved as-is)
# ---------------------------------------------------------------------------

def get_token() -> str:
    with open(TOKEN_PATH) as f:
        return f.read().strip()


def _k8s_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    """Generic K8s API call. Returns (status, parsed_json_or_None)."""
    try:
        ctx = ssl.create_default_context(cafile=CA_PATH)
        conn = http.client.HTTPSConnection(K8S_HOST, K8S_PORT, context=ctx, timeout=10)
        headers = {
            "Authorization": f"Bearer {get_token()}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=(json.dumps(body) if body else None), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return resp.status, data
    except Exception:
        return 0, None


def k8s_get(path: str) -> dict | None:
    status, data = _k8s_request("GET", path)
    return data if status == 200 else None


def k8s_get_text(path: str) -> str:
    """GET a plain-text K8s endpoint (e.g. pod logs). Returns '' on any error."""
    try:
        ctx = ssl.create_default_context(cafile=CA_PATH)
        conn = http.client.HTTPSConnection(K8S_HOST, K8S_PORT, context=ctx, timeout=10)
        conn.request("GET", path, headers={"Authorization": f"Bearer {get_token()}"})
        resp = conn.getresponse()
        raw = resp.read()
        return raw.decode("utf-8", "replace") if resp.status == 200 else ""
    except Exception:
        return ""


def k8s_post(path: str, body: dict) -> tuple[int, dict | None]:
    return _k8s_request("POST", path, body=body)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

class DB:
    """Thin postgres wrapper. Approvals features go through this; if any
    method fails, callers fall back to 'unavailable'."""

    @staticmethod
    def available() -> bool:
        if not HAVE_PSYCOPG:
            return False
        if not (DB_USER and DB_PASSWORD):
            return False
        try:
            with DB.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    @staticmethod
    def connect():  # type: ignore[no-untyped-def]
        return psycopg.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            autocommit=True, connect_timeout=5,
        )


# ---------------------------------------------------------------------------
# Remediator Job spec (duplicated from event_watcher.py — small, stable)
# ---------------------------------------------------------------------------

def build_remediator_job(investigation_id: str) -> dict:
    job_name = f"remediator-{investigation_id[:8]}"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": PLATFORM_HEALTH_AGENT_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "platform-health-agent-remediator",
                "app.kubernetes.io/part-of": "platform-health-agent",
                "investigation-id": investigation_id,
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": 3600,
            "activeDeadlineSeconds": 600,
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "platform-health-agent-remediator",
                        "investigation-id": investigation_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "platform-health-agent-writer",
                    "automountServiceAccountToken": True,
                    "nodeSelector": {"kubernetes.io/arch": "amd64"},
                    "initContainers": [
                        {
                            "name": "install-tools",
                            "image": "alpine:3.20",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                f"set -eu; cd /tools; "
                                f"echo 'fetching kubectl {KUBECTL_VERSION}'; "
                                f"wget -qO kubectl https://dl.k8s.io/release/{KUBECTL_VERSION}/bin/linux/amd64/kubectl && chmod +x kubectl; "
                                f"apk add -q curl bash; "
                                f"export HOME=/tools; "
                                f"curl -fsSL https://cli.kiro.dev/install | bash; "
                                # Move ALL three binaries (kiro-cli, kiro-cli-chat,
                                # kiro-cli-term). The launcher forks to kiro-cli-chat.
                                f"mv /tools/.local/bin/* /tools/ 2>/dev/null || true; "
                                f"chmod +x /tools/kiro-cli /tools/kiro-cli-chat /tools/kiro-cli-term 2>/dev/null || true; "
                                f"ls -la /tools/",
                            ],
                            "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
                            "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False,
                                                "capabilities": {"drop": ["ALL"]}},
                        },
                        {
                            "name": "install-pydeps",
                            "image": PYTHON_IMAGE,
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                "set -eu; pip install --no-cache-dir --target=/pydeps "
                                "psycopg[binary]==3.2.3 awslabs.eks-mcp-server",
                            ],
                            "volumeMounts": [{"name": "pydeps", "mountPath": "/pydeps"}],
                            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532,
                                                "allowPrivilegeEscalation": False,
                                                "capabilities": {"drop": ["ALL"]}},
                        },
                    ],
                    "containers": [{
                        "name": "remediator",
                        "image": PYTHON_IMAGE,
                        "command": ["/bin/sh", "/scripts/remediate.sh"],
                        "env": [
                            {"name": "INVESTIGATION_ID", "value": investigation_id},
                            {"name": "PATH", "value": "/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                            {"name": "PYTHONPATH", "value": "/pydeps"},
                            {"name": "HOME", "value": "/tmp"},
                            *[{"name": k, "valueFrom": {"configMapKeyRef": {"name": "platform-health-agent-config", "key": k}}}
                              for k in ["CLUSTER_NAME", "AWS_REGION", "DB_HOST", "DB_PORT", "DB_NAME",
                                        "KIRO_MODEL_REMEDIATE"]],
                            {"name": "DB_USER",      "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "username"}}},
                            {"name": "DB_PASSWORD",  "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "password"}}},
                            {"name": "KIRO_API_KEY", "valueFrom": {"secretKeyRef": {"name": "platform-health-agent-secrets",   "key": "KIRO_API_KEY"}}},
                        ],
                        "volumeMounts": [
                            {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                            {"name": "tools",   "mountPath": "/tools",   "readOnly": True},
                            {"name": "pydeps",  "mountPath": "/pydeps",  "readOnly": True},
                            {"name": "results", "mountPath": "/results"},
                            {"name": "tmp",     "mountPath": "/tmp"},
                        ],
                        "resources": {
                            "requests": {"cpu": "200m", "memory": "256Mi"},
                            "limits":   {"cpu": "1000m", "memory": "1Gi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }],
                    "volumes": [
                        {"name": "scripts", "configMap": {"name": "platform-health-agent-scripts", "defaultMode": 0o755}},
                        {"name": "tools",   "emptyDir": {}},
                        {"name": "pydeps",  "emptyDir": {}},
                        {"name": "results", "emptyDir": {}},
                        {"name": "tmp",     "emptyDir": {}},
                    ],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------

snapshot = {"nodes": [], "pods": [], "endpoints": [], "cost": {},
            "approvals_available": False, "approvals_pending": 0,
            "cluster": "", "region": "",
            "links": [], "ts": 0}
snapshot_lock = threading.Lock()


def _alb_hostname() -> str:
    """Discover the shared ALB hostname from the ingress status (empty until the
    AWS Load Balancer Controller provisions it)."""
    try:
        ing = k8s_get(
            f"/apis/networking.k8s.io/v1/namespaces/"
            f"{LINKS_INGRESS_NAMESPACE}/ingresses/{LINKS_INGRESS_NAME}"
        )
        return ((ing.get("status", {}).get("loadBalancer", {})
                 .get("ingress", [{}]) or [{}])[0].get("hostname", "")) or ""
    except Exception:
        return ""


def _build_links() -> list:
    """Quick links to the platform's web UIs. Prefers the public CloudFront edge
    URLs (set by Terraform into the cluster-dashboard-links ConfigMap when the
    edge is enabled) and falls back to the internal-ALB host:port for in-VPC /
    tunnel access. ArgoCD is an EKS capability with its own URL (ARGOCD_URL).
    Links with no resolvable URL are omitted."""
    alb = _alb_hostname()
    alb_url = (lambda port: f"http://{alb}:{port}" if alb else "")
    webui = os.environ.get("OPEN_WEBUI_URL", "") or alb_url(8080)
    litellm = os.environ.get("LITELLM_URL", "") or alb_url(4000)
    langfuse = os.environ.get("LANGFUSE_URL", "") or alb_url(3000)
    links = []
    if webui:
        links.append({"label": "Open WebUI", "url": webui,
                      "desc": "Chat with models", "icon": "💬"})
    if litellm:
        links.append({"label": "LiteLLM Admin", "url": litellm.rstrip("/") + "/ui/",
                      "desc": "API gateway + keys + usage", "icon": "🔑"})
    if langfuse:
        links.append({"label": "Langfuse", "url": langfuse,
                      "desc": "Traces, evals, cost", "icon": "📊"})
    if ARGOCD_URL:
        links.append({"label": "ArgoCD", "url": ARGOCD_URL,
                      "desc": "GitOps sync status", "icon": "🚢"})
    return links


# Serving kinds surfaced on the dashboard — the three tiers, all kro.run/v1alpha1
# in the `inference` namespace: VLLMEndpoint (simple), LLMDEndpoint (llm-d scale),
# LLMDDisaggEndpoint (llm-d scale + prefill/decode disaggregation).
SERVING_KINDS = [
    ("vllmendpoints", "vllm"),
    ("llmdendpoints", "llm-d"),
    ("llmddisaggendpoints", "llm-d-disagg"),
]


# Node hourly cost estimate — eu-central-1 on-demand list price ($/hr, Linux/
# Shared) from the AWS Pricing API. Spot nodes are approximated at SPOT_FACTOR of
# on-demand (real spot price varies). Unknown instance types are counted as
# `unpriced` so the total stays honest rather than silently undercounting.
# On-demand $/hr by region → instance type. Ships with eu-central-1 (the
# reference deployment). Other regions are supplied at runtime via an optional
# ConfigMap (see _prices_by_region) so the estimate works in any account/region
# without code changes. Unpriced instance types are counted and surfaced in the
# UI rather than guessed.
DEFAULT_PRICES_BY_REGION = {
    "eu-central-1": {
        "g6.xlarge": 1.0064, "g6.2xlarge": 1.2225, "g6.4xlarge": 1.6547, "g6.8xlarge": 2.519,
        "g6.12xlarge": 5.7543, "g6.16xlarge": 4.2477, "g6.24xlarge": 8.3473, "g6.48xlarge": 16.6946,
        "g5.xlarge": 1.258, "g5.2xlarge": 1.5156, "g5.4xlarge": 2.0308, "g5.8xlarge": 3.0612,
        "g5.12xlarge": 7.0928, "g5.48xlarge": 20.3681,
        "c5.large": 0.097, "c5.xlarge": 0.194, "c5.2xlarge": 0.388,
        "c5a.large": 0.087, "c5a.xlarge": 0.174,
        "c7i.large": 0.1018, "c7i.xlarge": 0.2037, "c7a.large": 0.1171, "c7a.xlarge": 0.2343,
        "m6g.large": 0.092, "m6g.xlarge": 0.184, "m5.large": 0.115, "m5.xlarge": 0.23, "r5.large": 0.152,
    },
}
SPOT_FACTOR = 0.35        # rough spot discount vs on-demand
HOURS_PER_MONTH = 730
NODE_PRICES_CM = os.environ.get("NODE_PRICES_CM", "cluster-dashboard-node-prices")
_price_cache: dict = {"ts": 0.0, "data": None}


def _prices_by_region() -> dict:
    """Region -> {instance: $/hr}. Merges the shipped defaults with an optional
    operator/Terraform-supplied ConfigMap (data['prices.json'] =
    {"<region>": {"<instance>": <usd_hourly>}}) so the cost estimate is not tied
    to a single account or region. Cached for an hour; best-effort."""
    now = time.time()
    if _price_cache["data"] is not None and now - _price_cache["ts"] < 3600:
        return _price_cache["data"]
    merged = {r: dict(v) for r, v in DEFAULT_PRICES_BY_REGION.items()}
    try:
        cm = k8s_get(f"/api/v1/namespaces/{LITELLM_NAMESPACE}/configmaps/{NODE_PRICES_CM}")
        raw = ((cm or {}).get("data") or {}).get("prices.json", "")
        for region, table in (json.loads(raw) if raw else {}).items():
            merged.setdefault(region, {}).update({k: float(v) for k, v in table.items()})
    except Exception:
        pass
    _price_cache.update(ts=now, data=merged)
    return merged


def _detect_region(nodes: list) -> str:
    """Most common node region label, falling back to the AWS_REGION env."""
    counts: dict = {}
    for n in nodes:
        r = n.get("region")
        if r:
            counts[r] = counts.get(r, 0) + 1
    return max(counts, key=counts.get) if counts else os.environ.get("AWS_REGION", "")


PRICING_ENDPOINT_REGION = os.environ.get("PRICING_ENDPOINT_REGION", "us-east-1")
_live_price_cache: dict = {}   # region -> {"ts": float, "prices": {instance: usd}}


def _fetch_ec2_ondemand(client, region: str, instance_type: str):
    """One instance type's on-demand Linux/shared $/hr from the Price List API."""
    resp = client.get_products(ServiceCode="AmazonEC2", MaxResults=1, Filters=[
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
    ])
    for item in resp.get("PriceList", []):
        terms = (json.loads(item).get("terms", {}) or {}).get("OnDemand", {}) or {}
        for term in terms.values():
            for dim in (term.get("priceDimensions", {}) or {}).values():
                usd = (dim.get("pricePerUnit", {}) or {}).get("USD")
                if usd and float(usd) > 0:
                    return round(float(usd), 4)
    return None


def _live_prices(region: str, instances: tuple) -> dict:
    """Live on-demand prices from the AWS Price List API (needs pricing:GetProducts
    via Pod Identity + boto3). Cached per region for 6h; missing types fetched on
    demand. Best-effort — returns {} if boto3/creds/perms are unavailable so the
    caller falls back to the shipped/ConfigMap table."""
    if not (HAVE_BOTO3 and region and instances):
        return {}
    ent = _live_price_cache.get(region)
    now = time.time()
    if ent is None or now - ent["ts"] > 21600:
        ent = {"ts": now, "prices": {}}
        _live_price_cache[region] = ent
    missing = [i for i in instances if i not in ent["prices"]]
    if missing:
        try:
            client = boto3.client("pricing", region_name=PRICING_ENDPOINT_REGION)
            for it in missing:
                try:
                    p = _fetch_ec2_ondemand(client, region, it)
                    if p is not None:
                        ent["prices"][it] = p
                except Exception:
                    pass
        except Exception:
            pass
    return dict(ent["prices"])


def _compute_cost(nodes: list) -> dict:
    """Sum the running nodes' hourly rates -> realtime $/hr + 730h monthly
    projection, split GPU vs platform. Region-aware; annotates each node with
    its `hourly`. Instance types with no price data are counted (`unpriced`).
    Prefers live AWS Price List data, falling back to the ConfigMap/shipped table."""
    region = _detect_region(nodes)
    instances = tuple(sorted({n.get("instance") for n in nodes
                              if n.get("instance") and n.get("instance") != "unknown"}))
    prices = dict(_prices_by_region().get(region, {}))
    live = _live_prices(region, instances)
    prices.update(live)
    price_source = "aws-price-list" if live else ("table" if prices else "none")
    hourly = gpu_hourly = 0.0
    unpriced = 0
    for n in nodes:
        base = prices.get(n["instance"])
        if base is None:
            unpriced += 1
            n["hourly"] = 0.0
            continue
        rate = base * (SPOT_FACTOR if n.get("capacity") == "spot" else 1.0)
        n["hourly"] = round(rate, 4)
        hourly += rate
        if n["gpu"] > 0:
            gpu_hourly += rate
    return {
        "hourly": round(hourly, 4),
        "monthly": round(hourly * HOURS_PER_MONTH, 2),
        "gpuHourly": round(gpu_hourly, 4),
        "platformHourly": round(hourly - gpu_hourly, 4),
        "unpriced": unpriced,
        "region": region,
        "priceSource": price_source,
    }


def _normalize_endpoint(ep: dict, mode: str) -> dict:
    """Flatten a serving CR (any of the three kinds) into one dashboard record
    with a `mode` field. Status is normalized so the UI can render all kinds
    uniformly: vLLM/llm-d report ready +
    availableReplicas; llm-d also reports routerHealth (EPP/router health)."""
    spec = ep.get("spec", {}) or {}
    status = ep.get("status", {}) or {}
    gpu_count = int(spec.get("gpuCount", 1) or 1)
    tp = int(spec.get("tensorParallelSize", 0) or 0)
    pp = int(spec.get("pipelineParallelSize", 1) or 1)
    if tp == 0:
        tp = gpu_count if pp == 1 else 1
    # Desired replicas: llm-d + vLLM use `replicas`;
    # llm-d-disagg splits into prefill + decode pools.
    is_disagg = mode == "llm-d-disagg"
    if is_disagg:
        prefill_n = int(spec.get("prefillReplicas", 1) or 1)
        decode_n = int(spec.get("decodeReplicas", 1) or 1)
        replicas = prefill_n + decode_n
    else:
        prefill_n = decode_n = None
        replicas = int(spec.get("replicas", spec.get("minReplicas", 1)) or 1)
    ready = str(status.get("ready", "False"))
    avail = (int(status.get("prefillReplicas", 0) or 0) + int(status.get("decodeReplicas", 0) or 0)) \
        if is_disagg else int(status.get("availableReplicas", 0) or 0)
    norm_status = "Running" if ready == "True" else "Pending"
    # KRO reconcile state (CR-level). An ERROR CR — e.g. a name collision where a
    # generated resource belongs to another endpoint's ApplySet — can still read
    # a colliding resource's replicas, so surface the real state rather than a
    # false "ready". Capture the failing Ready/ResourcesReady condition message.
    kro_state = str(status.get("state", "") or "")
    recon_err = ""
    if kro_state == "ERROR":
        norm_status = "Error"
        for c in (status.get("conditions", []) or []):
            if c.get("type") in ("Ready", "ResourcesReady") and str(c.get("status")) == "False":
                recon_err = " ".join((c.get("message", "") or "").split())[:300]
                break
    return {
        "name": ep["metadata"]["name"],
        "model": spec.get("model", ""),
        "mode": mode,
        "shared": bool(spec.get("shared", False)),
        "gpuCount": gpu_count,
        "tp": tp,
        "pp": pp,
        "replicas": replicas,
        "availableReplicas": avail,
        "ready": ready,
        # `modelStatus` kept for back-compat with the current HTML; `status` is
        # the normalized, mode-agnostic label the reworked UI should use.
        "modelStatus": norm_status,
        "status": norm_status,
        "routerHealth": str(status.get("routerHealth", "")) if mode in ("llm-d", "llm-d-disagg") else "",
        "message": "",
        "kroState": kro_state,
        "reconcileError": recon_err,
        "prefillReplicas": prefill_n,
        "decodeReplicas": decode_n,
        "created": ep["metadata"].get("creationTimestamp", ""),
    }


def _fetch_endpoints() -> list:
    """Poll every serving kind in the `inference` namespace and return normalized
    records. A kind whose CRD isn't installed is skipped silently."""
    out = []
    for plural, mode in SERVING_KINDS:
        data = k8s_get(f"/apis/kro.run/v1alpha1/namespaces/inference/{plural}")
        if not data:
            continue
        for ep in data.get("items", []):
            try:
                out.append(_normalize_endpoint(ep, mode))
            except Exception:
                continue
    out.sort(key=lambda e: (e["mode"], e["name"]))
    return out


def _cpu_m(s) -> int:
    """K8s CPU quantity -> millicores."""
    s = str(s or "0").strip()
    try:
        if s.endswith("m"): return int(float(s[:-1]))
        if s.endswith("n"): return int(float(s[:-1]) / 1e6)
        if s.endswith("u"): return int(float(s[:-1]) / 1e3)
        return int(float(s) * 1000)
    except Exception:
        return 0


def _mem_mi(s) -> int:
    """K8s memory quantity -> MiB."""
    s = str(s or "0").strip()
    for u, f in (("Ki", 1 / 1024), ("Mi", 1.0), ("Gi", 1024.0), ("Ti", 1048576.0),
                 ("K", 0.000953674), ("M", 0.953674), ("G", 953.674)):
        if s.endswith(u):
            try: return int(float(s[:-len(u)]) * f)
            except Exception: return 0
    try: return int(float(s) / 1048576)
    except Exception: return 0


def _build_k8s_snapshot() -> dict:
    nodes_data = k8s_get("/api/v1/nodes")
    pods_data = k8s_get("/api/v1/pods")

    nodes = []
    node_alerts = []
    if nodes_data:
        for n in nodes_data.get("items", []):
            meta = n.get("metadata", {}) or {}
            labels = meta.get("labels", {})
            nm = meta["name"]
            # A node being deprovisioned goes NotReady/unreachable by design:
            # Karpenter cordons + drains it (taint karpenter.sh/disrupted) and
            # sets a deletionTimestamp. Alerting on that false-positives on every
            # scale-down/consolidation, so skip health alerts while it terminates.
            terminating = bool(meta.get("deletionTimestamp")) or any(
                tt.get("key") == "karpenter.sh/disrupted"
                for tt in ((n.get("spec", {}) or {}).get("taints") or [])
            )
            conditions = n.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            # EKS node health: surface resource pressures + NotReady as alerts.
            if not terminating:
                for c in conditions:
                    t = c.get("type")
                    if t in ("MemoryPressure", "DiskPressure", "PIDPressure") and c.get("status") == "True":
                        node_alerts.append({"node": nm, "issue": t, "sev": "warn"})
            if not ready:
                if not terminating:
                    node_alerts.append({"node": nm, "issue": "NotReady", "sev": "warn"})
                continue
            allocatable = n.get("status", {}).get("allocatable", {})
            zone = labels.get("topology.kubernetes.io/zone", "")
            nodes.append({
                "name": nm,
                "instance": labels.get("node.kubernetes.io/instance-type", "unknown"),
                "pool": labels.get("karpenter.sh/nodepool",
                                   "__mng__" if labels.get("eks.amazonaws.com/nodegroup") else "__mng__"),
                "zone": zone,
                "region": labels.get("topology.kubernetes.io/region", zone[:-1] if zone else ""),
                "capacity": labels.get("karpenter.sh/capacity-type",
                                       labels.get("eks.amazonaws.com/capacityType", "on-demand")),
                "gpu": int(allocatable.get("nvidia.com/gpu", "0")),
                "gpuProduct": labels.get("nvidia.com/gpu.product", "").replace("-", " "),
                "cpuAllocM": _cpu_m(allocatable.get("cpu", "0")),
                "memAllocMi": _mem_mi(allocatable.get("memory", "0")),
                "created": n["metadata"].get("creationTimestamp", ""),
            })

    # Actual node usage from metrics-server (metrics.k8s.io). Best-effort: with
    # no metrics-server, cpuUsedM/memUsedMi stay None and the UI shows only
    # requested. cpu is nanocores/millicores, memory is Ki/Mi — reuse parsers.
    node_usage = {}
    try:
        mu = k8s_get("/apis/metrics.k8s.io/v1beta1/nodes")
        for it in (mu or {}).get("items", []):
            u = it.get("usage", {}) or {}
            node_usage[it.get("metadata", {}).get("name", "")] = {
                "cpuUsedM": _cpu_m(u.get("cpu", "0")),
                "memUsedMi": _mem_mi(u.get("memory", "0")),
            }
    except Exception:
        node_usage = {}
    for n in nodes:
        n["cpuUsedM"] = node_usage.get(n["name"], {}).get("cpuUsedM")
        n["memUsedMi"] = node_usage.get(n["name"], {}).get("memUsedMi")

    pods = []
    if pods_data:
        for p in pods_data.get("items", []):
            phase = p.get("status", {}).get("phase", "")
            if phase not in ("Running", "Pending"):
                continue
            containers = p.get("spec", {}).get("containers", [])
            reqs = [c.get("resources", {}).get("requests", {}) or {} for c in containers]
            gpu_req = sum(int(r.get("nvidia.com/gpu", "0") or 0) for r in reqs)
            cpu_req = sum(_cpu_m(r.get("cpu", "0")) for r in reqs)
            mem_req = sum(_mem_mi(r.get("memory", "0")) for r in reqs)
            pods.append({
                "name": p["metadata"]["name"],
                "namespace": p["metadata"]["namespace"],
                "node": p.get("spec", {}).get("nodeName", ""),
                "phase": phase,
                "gpu": gpu_req,
                "cpuReqM": cpu_req,
                "memReqMi": mem_req,
                "created": p["metadata"].get("creationTimestamp", ""),
            })

    endpoints = _fetch_endpoints()
    cost = _compute_cost(nodes)

    # Approvals snapshot — best-effort, never blocks the dashboard.
    approvals_available = False
    approvals_pending = 0
    if HAVE_PSYCOPG and DB_USER and DB_PASSWORD:
        try:
            with DB.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM investigations WHERE status='awaiting_approval'")
                approvals_pending = int(cur.fetchone()[0] or 0)  # type: ignore[index]
                approvals_available = True
        except Exception:
            approvals_available = False

    return {
        "nodes": nodes, "pods": pods, "endpoints": endpoints, "nodeAlerts": node_alerts,
        "cost": cost,
        "approvals_available": approvals_available,
        "approvals_pending": approvals_pending,
        "cluster": os.environ.get("CLUSTER_NAME", ""),
        "region": cost.get("region", "") or os.environ.get("AWS_REGION", ""),
        "links": _build_links(),
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# v2 metrics layer — LiteLLM DB (traffic/latency/spend/teams/health) + DCGM GPU
# utilization + ArgoCD sync + Bedrock model list. All in-cluster; each source is
# best-effort and reported in `sources` for graceful degradation in the UI.
# ---------------------------------------------------------------------------

LITELLM_DB_NAME    = os.environ.get("LITELLM_DB_NAME", "litellm")
DCGM_URL           = os.environ.get("DCGM_URL", "http://nvidia-dcgm-exporter.gpu-operator.svc.cluster.local:9400/metrics")
DCGM_NAMESPACE     = os.environ.get("DCGM_NAMESPACE", "gpu-operator")
DCGM_POD_SELECTOR  = os.environ.get("DCGM_POD_SELECTOR", "app=nvidia-dcgm-exporter")
DCGM_PORT          = os.environ.get("DCGM_PORT", "9400")
LITELLM_CONFIG_CM  = os.environ.get("LITELLM_CONFIG_CM", "litellm-config")
LITELLM_NAMESPACE  = os.environ.get("LITELLM_NAMESPACE", "ai-platform")
METRICS_INTERVAL   = int(os.environ.get("METRICS_INTERVAL", "25"))

try:
    import yaml as _yaml
    HAVE_YAML = True
except Exception:
    HAVE_YAML = False

metrics = {"models": [], "teams": [], "gpu": [], "argocd": [],
           "costExt": {}, "sources": {}, "mts": 0}
metrics_lock = threading.Lock()


def _lldb_connect():  # type: ignore[no-untyped-def]
    return psycopg.connect(host=DB_HOST, port=DB_PORT, dbname=LITELLM_DB_NAME,
                           user=DB_USER, password=DB_PASSWORD,
                           autocommit=True, connect_timeout=5)


def _litellm_metrics() -> dict:
    """Per-model 5-min metrics + model health + per-team spend/budgets + cost trend."""
    out = {"perModel": {}, "teams": [], "trend": [], "health": {}, "byTeam": [], "byUser": []}
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return out
    with _lldb_connect() as conn, conn.cursor() as cur:
        cur.execute("""
          SELECT model_group, custom_llm_provider, count(*)/5.0,
                 percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM ("endTime"-"startTime"))*1000),
                 percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM ("endTime"-"startTime"))*1000),
                 COALESCE(sum(total_tokens),0)/300.0, COALESCE(sum(spend),0),
                 100.0*avg(CASE WHEN status='success' THEN 0 ELSE 1 END)
          FROM "LiteLLM_SpendLogs" WHERE "startTime" > now() - interval '5 minutes'
          GROUP BY model_group, custom_llm_provider""")
        for r in cur.fetchall():
            out["perModel"][r[0]] = {"provider": r[1], "rpm": round(r[2], 1),
                "p50": round(r[3]) if r[3] is not None else None,
                "p95": round(r[4]) if r[4] is not None else None,
                "tps": round(r[5], 1), "spend5m": float(r[6]),
                "errPct": round(float(r[7]), 1) if r[7] is not None else 0}
        try:
            cur.execute('SELECT DISTINCT ON (model_name) model_name,status,response_time_ms FROM "LiteLLM_HealthCheckTable" ORDER BY model_name,checked_at DESC')
            for r in cur.fetchall():
                out["health"][r[0]] = {"status": r[1], "rt": float(r[2]) if r[2] is not None else None}
        except Exception:
            pass
        try:
            cur.execute("""
              SELECT t.team_id, COALESCE(t.team_alias, left(t.team_id::text,8)), t.max_budget, COALESCE(t.spend,0),
                     COALESCE(sum(s.api_requests),0), COALESCE(sum(s.failed_requests),0),
                     COALESCE(sum(s.spend),0), COALESCE(sum(s.prompt_tokens+s.completion_tokens),0)
              FROM "LiteLLM_TeamTable" t
              LEFT JOIN "LiteLLM_DailyTeamSpend" s ON s.team_id=t.team_id AND s.date=CURRENT_DATE::text
              GROUP BY t.team_id, t.team_alias, t.max_budget, t.spend""")
            teams = {r[0]: {"id": r[0], "name": r[1], "budget": round(float(r[3]), 2),
                            "limit": float(r[2]) if r[2] else None, "reqsToday": int(r[4]),
                            "failed": int(r[5]), "spendToday": round(float(r[6]), 2),
                            "tokens": int(r[7]), "keys": 0} for r in cur.fetchall()}
            try:
                cur.execute('SELECT team_id,count(*) FROM "LiteLLM_VerificationToken" WHERE team_id IS NOT NULL GROUP BY team_id')
                for tid, c in cur.fetchall():
                    if tid in teams:
                        teams[tid]["keys"] = int(c)
            except Exception:
                pass
            out["teams"] = list(teams.values())
            out["byTeam"] = sorted([[t["name"], t["spendToday"]] for t in out["teams"] if t["spendToday"] > 0], key=lambda x: -x[1])
        except Exception:
            pass
        try:
            cur.execute('SELECT date::text, COALESCE(sum(spend),0) FROM "LiteLLM_DailyTeamSpend" GROUP BY date ORDER BY date DESC LIMIT 14')
            out["trend"] = [[r[0], round(float(r[1]), 2)] for r in cur.fetchall()][::-1]
        except Exception:
            pass
        try:
            # Per-user cost/usage (30d). Open WebUI forwards the signed-in user
            # as an x-openwebui-user-email request tag (ENABLE_FORWARD_USER_INFO_HEADERS
            # + LiteLLM extra_spend_tag_headers), captured in request_tags (jsonb).
            # NOTE: only covers interactive (Open WebUI) users; direct API callers
            # are attributed by key/team, not user.
            cur.execute("""
              SELECT split_part(tag, ': ', 2) AS u, COALESCE(model_group, '?'),
                     count(*), COALESCE(sum(total_tokens),0), COALESCE(sum(spend),0)
              FROM "LiteLLM_SpendLogs", jsonb_array_elements_text(request_tags) tag
              WHERE "startTime" > now() - interval '30 days'
                AND tag LIKE 'x-openwebui-user-email:%'
              GROUP BY 1, 2""")
            agg = {}
            for u, model, reqs, toks, spend in cur.fetchall():
                e = agg.setdefault(u, {"user": u, "requests": 0, "tokens": 0, "spend": 0.0, "models": {}})
                e["requests"] += int(reqs); e["tokens"] += int(toks); e["spend"] += float(spend)
                e["models"][model] = e["models"].get(model, 0.0) + float(spend)
            out["byUser"] = sorted(
                [{"user": e["user"], "requests": e["requests"], "tokens": e["tokens"],
                  "spend": round(e["spend"], 4),
                  "models": sorted([{"model": m, "spend": round(s, 4)} for m, s in e["models"].items()],
                                   key=lambda x: -x["spend"])}
                 for e in agg.values()], key=lambda x: -x["spend"])[:50]
        except Exception:
            pass
    return out


def _dcgm_util() -> dict:
    """Per-node GPU utilization AND framebuffer-memory % from dcgm-exporter.

    Returns {host: {"util": [pct-per-gpu], "mem": [pct-per-gpu]}}. Scrapes EVERY
    exporter pod directly (one per GPU node), not the round-robin Service VIP —
    the VIP returns only one pod's GPUs per scrape. Falls back to the Service URL
    if pod discovery yields nothing."""
    import urllib.request
    METRICS = ("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_FB_FREE")

    def _parse(text: str, acc: dict) -> None:
        for line in text.splitlines():
            for m in METRICS:
                if line.startswith(m + "{"):
                    try:
                        labels = line[line.index("{") + 1:line.index("}")]
                        val = float(line.rsplit(" ", 1)[1])
                        kv = dict(x.split("=", 1) for x in labels.split(",") if "=" in x)
                        host = kv.get("Hostname", "").strip('"')
                        gpu = kv.get("gpu", "0").strip('"')
                        if host:
                            acc.setdefault(host, {}).setdefault(gpu, {})[m] = val
                    except Exception:
                        pass
                    break

    def _scrape(url: str, acc: dict) -> None:
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=4) as r:
                _parse(r.read().decode("utf-8", "ignore"), acc)
        except Exception:
            pass

    acc: dict = {}
    urls = []
    try:
        pods = k8s_get(f"/api/v1/namespaces/{DCGM_NAMESPACE}/pods"
                       f"?labelSelector={DCGM_POD_SELECTOR}")
        for p in (pods or {}).get("items", []):
            st = p.get("status", {})
            if st.get("phase") == "Running" and st.get("podIP"):
                urls.append(f"http://{st['podIP']}:{DCGM_PORT}/metrics")
    except Exception:
        urls = []
    for url in urls:
        _scrape(url, acc)
    if not acc:
        _scrape(DCGM_URL, acc)

    out: dict = {}
    for host, gpus in acc.items():
        util, mem = [], []
        for g in sorted(gpus, key=lambda x: int(x) if str(x).isdigit() else 0):
            d = gpus[g]
            util.append(round(d.get("DCGM_FI_DEV_GPU_UTIL", 0)))
            used, free = d.get("DCGM_FI_DEV_FB_USED"), d.get("DCGM_FI_DEV_FB_FREE")
            mem.append(round(used / (used + free) * 100)
                       if (used is not None and free is not None and (used + free) > 0) else None)
        out[host] = {"util": util, "mem": mem}
    return out


def _bedrock_models() -> list:
    """Declared Bedrock (config-defined) models from the litellm-config ConfigMap."""
    if not HAVE_YAML:
        return []
    cm = k8s_get(f"/api/v1/namespaces/{LITELLM_NAMESPACE}/configmaps/{LITELLM_CONFIG_CM}")
    if not cm:
        return []
    try:
        cfg = _yaml.safe_load((cm.get("data") or {}).get("config.yaml", "")) or {}
        out = []
        for m in (cfg.get("model_list") or []):
            mdl = str((m.get("litellm_params") or {}).get("model", ""))
            if mdl.startswith("bedrock/"):
                out.append({"name": m.get("model_name", ""), "model": mdl})
        return out
    except Exception:
        return []


# Karpenter/provisioning Warning reasons that explain a stuck GPU deploy.
_PROVISION_HINT = ("capacity", "fulfill", "launch", "zonal", "quota")


def _events_list(path: str, kind: str = "") -> list:
    """Fetch + flatten K8s Events at `path`, optionally filtered to a kind."""
    d = k8s_get(path)
    out = []
    for e in (d or {}).get("items", []):
        io = e.get("involvedObject", {}) or {}
        if kind and io.get("kind") != kind:
            continue
        out.append({
            "type": e.get("type", ""),
            "reason": e.get("reason", ""),
            "message": " ".join((e.get("message", "") or "").split())[:220],
            "count": int(e.get("count", 1) or 1),
            "obj": io.get("name", ""),
            "ts": e.get("lastTimestamp") or e.get("eventTime") or "",
            "by": (e.get("source", {}) or {}).get("component", "") or e.get("reportingComponent", ""),
        })
    return out


AUTOSCALING_PROM_URL = os.environ.get(
    "AUTOSCALING_PROM_URL", "http://autoscaling-prometheus.inference.svc.cluster.local:9090")


def _prom_query(promql: str) -> list:
    """Instant query against the always-on autoscaling Prometheus. Returns the
    result vector [{metric:{...}, value:[ts,'v']}] or [] on any failure."""
    import urllib.request
    import urllib.parse
    url = AUTOSCALING_PROM_URL + "/api/v1/query?query=" + urllib.parse.quote(promql)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=4) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return d.get("data", {}).get("result", []) if d.get("status") == "success" else []
    except Exception:
        return []


def _quantity(q) -> float:
    """Parse a k8s quantity string (e.g. '144', '900m', '2') to float."""
    s = str(q if q is not None else "0").strip()
    try:
        if s.endswith("m"):
            return float(s[:-1]) / 1000.0
        return float(s)
    except Exception:
        return 0.0


def _autoscaling_status(endpoints: list, hpa_events: list) -> list:
    """Per-pool KEDA/HPA autoscaling state — so admins see each pool scaling
    INDEPENDENTLY and where it's bottlenecked. For every keda-hpa-* in the
    `inference` namespace it reports current/desired within min..max, the driving
    saturation signal vs its KEDA target (prefill & llm-d -> queue depth; decode
    -> KV-cache %), whether it's capped at max (a real capacity bottleneck), the
    last scale time, and recent scale events. Metric value + target come straight
    from the HPA (KEDA publishes them there), so no extra scrape is needed.

    Pool + role are derived from the scale-target Deployment suffix:
      <pool>-prefill -> disagg prefill,  <pool>-decode -> disagg decode,
      <pool>-llmd    -> llm-d single pool.
    """
    hpas = k8s_get("/apis/autoscaling/v2/namespaces/inference/horizontalpodautoscalers")
    if not hpas:
        return []
    mode_by_pool = {ep["name"]: ep.get("mode", "") for ep in endpoints}
    # Per-pool p95 latency attribution (prefill -> TTFT, decode -> ITL). vLLM
    # exposes these as histograms; quantile them grouped by (pool, role).
    def _q95(metric: str) -> dict:
        b: dict = {}
        for s in _prom_query(f"histogram_quantile(0.95, sum by (le,pool,role) (rate({metric}_bucket[5m])))"):
            m = s.get("metric", {})
            try:
                v = float(s["value"][1])
                if v == v:  # drop NaN (no samples in window)
                    b[(m.get("pool", ""), m.get("role", ""))] = v
            except Exception:
                pass
        return b

    def _pick(b: dict, pool: str, role: str):
        if (pool, role) in b:
            return b[(pool, role)]
        # llm-d single pool labels its pods role=decode while its HPA target is
        # <name>-llmd (role ""); fall back to any role for the same pool.
        for (p, _r), v in b.items():
            if p == pool:
                return v
        return None

    ttft = _q95("vllm:time_to_first_token_seconds")
    itl = _q95("vllm:inter_token_latency_seconds")
    rescales: dict = {}
    for e in hpa_events:
        if e.get("reason") == "SuccessfulRescale":
            rescales.setdefault(e.get("obj", ""), []).append({"ts": e.get("ts", ""), "msg": e.get("message", "")})
    out = []
    for h in hpas.get("items", []):
        meta = h.get("metadata", {}) or {}
        spec = h.get("spec", {}) or {}
        st = h.get("status", {}) or {}
        hname = meta.get("name", "")
        dep = (spec.get("scaleTargetRef", {}) or {}).get("name", "")
        if dep.endswith("-prefill"):
            pool, role, sig, unit = dep[:-len("-prefill")], "prefill", "queue depth", "req"
        elif dep.endswith("-decode"):
            pool, role, sig, unit = dep[:-len("-decode")], "decode", "KV-cache", "%"
        elif dep.endswith("-llmd"):
            pool, role, sig, unit = dep[:-len("-llmd")], "", "queue depth", "req"
        else:
            pool, role, sig, unit = dep, "", "queue depth", "req"
        # Driving signal = the external metric with the highest value/target ratio
        # (the one actually pushing the scale decision — i.e. the bottleneck).
        best = None  # (value, target, ratio)
        m_specs = spec.get("metrics", []) or []
        m_curs = st.get("currentMetrics", []) or []
        for i, ms in enumerate(m_specs):
            tgt = _quantity((((ms.get("external", {}) or {}).get("target", {}) or {}).get("averageValue")))
            val = 0.0
            if i < len(m_curs):
                val = _quantity(((((m_curs[i] or {}).get("external", {}) or {}).get("current", {}) or {}).get("averageValue")))
            ratio = (val / tgt) if tgt > 0 else 0.0
            if best is None or ratio > best[2]:
                best = (val, tgt, ratio)
        val, tgt, _ratio = best or (0.0, 0.0, 0.0)
        _tt = _pick(ttft, pool, role)
        _it = _pick(itl, pool, role)
        mn = int(spec.get("minReplicas", 1) or 1)
        mx = int(spec.get("maxReplicas", 1) or 1)
        cur = int(st.get("currentReplicas", 0) or 0)
        des = int(st.get("desiredReplicas", 0) or 0)
        out.append({
            "pool": pool, "role": role, "mode": mode_by_pool.get(pool, ""),
            "deployment": dep, "signal": sig, "unit": unit,
            "value": round(val, 2), "target": round(tgt, 2),
            "ttftMs": round(_tt * 1000) if _tt else None,
            "itlMs": round(_it * 1000, 1) if _it else None,
            "min": mn, "max": mx, "current": cur, "desired": des,
            # atMax: pinned at ceiling AND wanting more -> add capacity here.
            "atMax": des >= mx and mx > mn,
            # saturated: driving signal within 10% of its target (scaling pressure).
            "saturated": tgt > 0 and val >= 0.9 * tgt,
            "lastScale": st.get("lastScaleTime", ""),
            "events": sorted(rescales.get(hname, []), key=lambda x: x["ts"], reverse=True)[:6],
        })
    out.sort(key=lambda x: (x["pool"], x["role"]))
    return out


def _deploy_diagnostics(models: list, pods: list) -> list:
    """Explain why non-running self-hosted models aren't serving yet. Attaches a
    `deploy` block to each (pod phase, container waiting reason, recent
    scheduling events, model-load log tail) and returns cluster-level GPU
    provisioning warnings (Karpenter capacity errors) for the alert feed.

    All calls are best-effort; a failing source degrades to empty, never raises.
    Targeted per-pod GETs only run for models that aren't running (typically
    0-2 at a time), so this stays cheap on the poll loop."""
    # Cluster GPU provisioning warnings — durable NodeClaim events survive the
    # create->UnfulfillableCapacity->delete retry loop that Karpenter runs.
    provisioning, seen = [], set()
    for e in _events_list("/api/v1/events?fieldSelector=involvedObject.kind=NodeClaim", "NodeClaim"):
        if e["type"] != "Warning" or not any(h in e["reason"].lower() for h in _PROVISION_HINT):
            continue
        key = (e["reason"], e["message"][:60])
        if key in seen:
            continue
        seen.add(key)
        provisioning.append(e)
    provisioning = provisioning[:5]

    # Pod events in the inference namespace, indexed by pod name.
    ev_index: dict = {}
    for e in _events_list("/api/v1/namespaces/inference/events?fieldSelector=involvedObject.kind=Pod", "Pod"):
        ev_index.setdefault(e["obj"], []).append(e)

    for m in models:
        if m.get("status") == "running" or m.get("tier") == "bedrock":
            continue
        name = m["name"]
        # The serving pod(s) — skip the transient LiteLLM register job pod.
        cand = [p for p in pods if p.get("namespace") == "inference"
                and p["name"].startswith(name + "-") and "-register" not in p["name"]]
        cand.sort(key=lambda p: ((p.get("gpu", 0) or 0) > 0, p.get("created", "")), reverse=True)
        dep = {"phase": "NoPod", "pod": "", "waiting": None, "events": [],
               "provisioning": [], "log": []}
        if cand:
            pod = cand[0]
            dep["pod"] = pod["name"]
            dep["phase"] = pod.get("phase", "")
            gpu_pod = (pod.get("gpu", 0) or 0) > 0
            evs = ev_index.get(pod["name"], [])
            unschedulable = any(e["reason"] == "FailedScheduling" for e in evs)
            # Keep the most useful few events: warnings first, newest first,
            # deduped by reason.
            evs_sorted = sorted(evs, key=lambda e: (e["type"] != "Warning", e["ts"]), reverse=True)
            picked, reasons = [], set()
            for e in evs_sorted:
                if e["reason"] in reasons:
                    continue
                reasons.add(e["reason"])
                picked.append(e)
                if len(picked) >= 4:
                    break
            dep["events"] = picked
            # A Pending, unschedulable GPU pod is stuck on capacity — surface it.
            if pod.get("phase") == "Pending" and unschedulable and gpu_pod and provisioning:
                dep["provisioning"] = provisioning
            # Container waiting reason + model-loading log tail (best-effort).
            pj = k8s_get(f"/api/v1/namespaces/inference/pods/{pod['name']}")
            if pj:
                st = pj.get("status", {}) or {}
                cstats = st.get("containerStatuses", []) or []
                ready = False
                cname = cstats[0].get("name") if cstats else ""
                for c in cstats:
                    w = (c.get("state", {}) or {}).get("waiting")
                    if w and not dep["waiting"]:
                        dep["waiting"] = {"reason": w.get("reason", ""),
                                          "message": " ".join((w.get("message", "") or "").split())[:200]}
                    ready = ready or bool(c.get("ready"))
                # Pod is up but the model isn't serving yet → show load progress.
                if st.get("phase") == "Running" and not ready and cname:
                    txt = k8s_get_text(
                        f"/api/v1/namespaces/inference/pods/{pod['name']}/log"
                        f"?tailLines=14&container={cname}")
                    if txt:
                        dep["log"] = [ln for ln in txt.splitlines() if ln.strip()][-14:]
        m["deploy"] = dep
    return provisioning


def _build_metrics(nodes: list, pods: list) -> dict:
    endpoints = _fetch_endpoints()
    src: dict = {}
    try:
        met = _litellm_metrics(); src["litellm"] = True
    except Exception:
        met = {"perModel": {}, "teams": [], "trend": [], "health": {}, "byTeam": [], "byUser": []}; src["litellm"] = False
    try:
        bedrock = _bedrock_models(); src["bedrock"] = True
    except Exception:
        bedrock = []; src["bedrock"] = False

    node_by_name = {n["name"]: n for n in nodes}
    def cost_for(name: str) -> float:
        c = 0.0
        for p in pods:
            if p.get("namespace") == "inference" and p["name"].startswith(name + "-") and p.get("gpu", 0) > 0:
                nd = node_by_name.get(p.get("node"))
                if nd and nd.get("gpu"):
                    c += nd.get("hourly", 0) * p["gpu"] / nd["gpu"]
        return round(c, 2)

    models = []
    for ep in endpoints:
        tier = ep["mode"]
        models.append({"name": ep["name"], "model": ep["model"], "tier": tier,
            "status": "error" if ep.get("kroState") == "ERROR" else ("running" if ep["ready"] == "True" else "deploying"),
            "gpu": ("shared" if ep.get("shared") else f'{ep["gpuCount"]}×GPU'),
            "replicas": ep.get("replicas"), "availableReplicas": ep.get("availableReplicas"),
            "router": ep.get("routerHealth", ""), "costHr": cost_for(ep["name"]),
            "reconcileError": ep.get("reconcileError", ""),
            "prefillReplicas": ep.get("prefillReplicas"), "decodeReplicas": ep.get("decodeReplicas"),
            "created": ep.get("created")})
    for b in bedrock:
        models.append({"name": b["name"], "model": b["model"], "tier": "bedrock",
            "status": "running", "gpu": "—", "replicas": None, "availableReplicas": None,
            "router": "", "costHr": 0.0, "created": None})
    for m in models:
        pm = met["perModel"].get(m["name"])
        m["rpm"] = pm["rpm"] if pm else 0
        m["p50"] = pm["p50"] if pm else None
        m["p95"] = pm["p95"] if pm else None
        m["tps"] = pm["tps"] if pm else 0
        m["errPct"] = pm["errPct"] if pm else 0
        if pm and m["tier"] == "bedrock":
            m["costHr"] = round(pm["spend5m"] * 12, 2)
        h = met["health"].get(m["name"])
        m["health"] = h["status"] if h else ""
    models.sort(key=lambda x: (-(x.get("rpm") or 0), x["name"]))

    gpu = []
    try:
        util = _dcgm_util(); src["dcgm"] = True
        for n in nodes:
            if n.get("gpu", 0) > 0:
                host = n["name"].split(".")[0]
                e = util.get(host) or util.get(n["name"]) or {}
                us = e.get("util") or [0] * n["gpu"]
                mem = e.get("mem") or [None] * n["gpu"]
                mdl = next((ep["name"] for ep in endpoints for p in pods
                            if p.get("node") == n["name"] and p.get("gpu", 0) > 0
                            and p["name"].startswith(ep["name"] + "-")), None)
                gpu.append({"node": host, "instance": n["instance"], "util": us, "mem": mem, "model": mdl})
    except Exception:
        src["dcgm"] = False

    argo = []
    try:
        d = k8s_get("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications")
        if d is not None:
            for a in d.get("items", []):
                st = a.get("status", {})
                argo.append({"name": a["metadata"]["name"],
                             "sync": st.get("sync", {}).get("status", "?"),
                             "health": st.get("health", {}).get("status", "?")})
            src["argocd"] = True
        else:
            src["argocd"] = False
    except Exception:
        src["argocd"] = False

    # Deployment diagnostics: explain any not-yet-running model + collect
    # cluster GPU provisioning warnings. Best-effort; never blocks the payload.
    try:
        provisioning = _deploy_diagnostics(models, pods)
    except Exception:
        provisioning = []

    by_model = sorted([[m["name"], m["costHr"], m["tier"]] for m in models if m["costHr"] > 0], key=lambda x: -x[1])
    cost_ext = {"byModel": by_model, "byTeam": met.get("byTeam", []), "trend": met.get("trend", []), "byUser": met.get("byUser", [])}
    # Autoscaling: per-pool KEDA/HPA state + bottleneck signal, attached to each
    # model (disagg models get two entries: prefill + decode, scaling
    # independently). Replaces the now-deprecated spec replica counts with the
    # live HPA reality.
    try:
        hpa_events = _events_list("/api/v1/namespaces/inference/events", "HorizontalPodAutoscaler")
        autoscaling = _autoscaling_status(endpoints, hpa_events)
        src["autoscaling"] = True
    except Exception:
        autoscaling, src["autoscaling"] = [], False
    by_pool: dict = {}
    for a in autoscaling:
        by_pool.setdefault(a["pool"], []).append(a)
    for m in models:
        sc = by_pool.get(m["name"])
        if sc:
            m["scale"] = sc
            m["availableReplicas"] = sum(x["current"] for x in sc)  # live running
            m["replicas"] = sum(x["desired"] for x in sc)           # what the HPAs want now

    return {"models": models, "teams": met.get("teams", []), "gpu": gpu, "argocd": argo,
            "autoscaling": autoscaling,
            "costExt": cost_ext, "provisioning": provisioning, "sources": src, "mts": time.time()}


def metrics_loop() -> None:
    global metrics
    while True:
        try:
            with snapshot_lock:
                nodes = list(snapshot.get("nodes", [])); pods = list(snapshot.get("pods", []))
            m = _build_metrics(nodes, pods)
            with metrics_lock:
                metrics = m
        except Exception:
            pass
        time.sleep(METRICS_INTERVAL)


def poll_loop() -> None:
    global snapshot
    while True:
        try:
            new_snapshot = _build_k8s_snapshot()
            with snapshot_lock:
                snapshot = new_snapshot
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Approvals API
# ---------------------------------------------------------------------------

def list_pending_investigations() -> list[dict]:
    """Return investigations awaiting approval (newest first)."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return []
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute(
                """SELECT id, created_at, trigger_kind, resource_kind,
                          resource_namespace, resource_name,
                          findings, fix_commands
                     FROM investigations
                    WHERE status = 'awaiting_approval'
                    ORDER BY created_at DESC LIMIT 50""",
            )
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            r["id"] = str(r["id"])
            r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
            out.append(r)
        return out
    except Exception:
        return []


def list_all_investigations(limit: int = 20) -> list[dict]:
    """Return recent investigations (any status), newest first.
    Used by the History tab in the dashboard."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return []
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute(
                """SELECT id, created_at, completed_at, approved_at, status,
                          trigger_kind, resource_kind, resource_namespace, resource_name,
                          findings, fix_commands, remediation_result,
                          approved_by, error_message, out_of_scope
                     FROM investigations
                    ORDER BY created_at DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall() or []
        out = []
        stale_cutoff = time.time() - STALE_INVESTIGATION_MINUTES * 60
        for r in rows:
            r["id"] = str(r["id"])
            # An in-flight row whose Job should long since have finished is stuck —
            # flag it so the UI can offer a Dismiss action (see dismiss_investigation).
            in_flight = r["status"] in ("pending", "running", "remediating")
            created = r.get("created_at")
            r["stale"] = bool(in_flight and created and created.timestamp() < stale_cutoff)
            for k in ("created_at", "completed_at", "approved_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
            out.append(r)
        return out
    except Exception:
        return []


def get_investigation_detail(investigation_id: str) -> dict | None:
    """Return full row for a single investigation. Used for post-approve polling."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return None
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute("SELECT * FROM investigations WHERE id = %s", (investigation_id,))
            r = cur.fetchone()
        if not r:
            return None
        r["id"] = str(r["id"])
        for k in ("created_at", "completed_at", "approved_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
        return r
    except Exception:
        return None


def approve_investigation(investigation_id: str, approver: str) -> tuple[int, dict]:
    """Spawn the Remediator Job for the given investigation. Returns
    (HTTP status, response body)."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}

    try:
        with DB.connect() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
                cur.execute("SELECT * FROM investigations WHERE id=%s", (investigation_id,))
                inv = cur.fetchone()
                if not inv:
                    return 404, {"error": "not found"}
                if inv["status"] != "awaiting_approval":
                    return 409, {"error": f"status is {inv['status']}, cannot approve"}
                # Expiry check.
                age_hrs = (time.time() - inv["created_at"].timestamp()) / 3600
                if age_hrs > APPROVAL_EXPIRY_HOURS:
                    cur.execute("UPDATE investigations SET status='expired', completed_at=now() WHERE id=%s",
                                (investigation_id,))
                    return 409, {"error": "expired"}
                # Daily cap.
                cur.execute("SELECT remediations FROM today_counters")
                today = (cur.fetchone() or {}).get("remediations", 0) or 0
                if today >= MAX_REMEDIATIONS_PER_DAY:
                    return 429, {"error": f"daily remediation budget exceeded ({today}/{MAX_REMEDIATIONS_PER_DAY})"}

                # Spawn the Job.
                job = build_remediator_job(investigation_id)
                status, body = k8s_post(
                    f"/apis/batch/v1/namespaces/{PLATFORM_HEALTH_AGENT_NAMESPACE}/jobs", job)
                if status not in (200, 201, 202):
                    return 502, {"error": "job create failed",
                                 "status": status,
                                 "body": (body or {}).get("message") or str(body)[:500]}

                # Update DB.
                cur.execute(
                    """UPDATE investigations
                          SET status='remediating',
                              approved_by=%s,
                              approved_at=now()
                        WHERE id=%s""",
                    (approver, investigation_id),
                )
                cur.execute(
                    """INSERT INTO daily_counters (day, remediations) VALUES (CURRENT_DATE, 1)
                       ON CONFLICT (day) DO UPDATE SET remediations = daily_counters.remediations + 1""",
                )
        return 200, {"ok": True, "investigation_id": investigation_id, "status": "remediating"}
    except Exception as e:
        return 500, {"error": str(e)}


def dismiss_investigation(investigation_id: str, approver: str) -> tuple[int, dict]:
    """Mark an investigation dismissed. Two paths:
      - 'awaiting_approval' rows: the user declined the proposed fix (normal case).
      - stale in-flight rows ('pending'/'running'/'remediating' older than
        STALE_INVESTIGATION_MINUTES): the Job died without writing a result, so
        the row is stuck — let the user clear it. Fresh in-flight rows are kept,
        so we never dismiss a genuinely active investigation."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute("SELECT status, created_at FROM investigations WHERE id=%s", (investigation_id,))
            inv = cur.fetchone()
            if not inv:
                return 404, {"error": "not found"}
            status = inv["status"]
            if status == "awaiting_approval":
                pass  # always dismissable
            elif status in ("pending", "running", "remediating"):
                age_min = (time.time() - inv["created_at"].timestamp()) / 60
                if age_min < STALE_INVESTIGATION_MINUTES:
                    return 409, {"error": "investigation still in progress"}
            else:
                return 409, {"error": f"status is {status}, cannot dismiss"}
            cur.execute(
                """UPDATE investigations
                      SET status='dismissed',
                          approved_by=%s,
                          approved_at=now(),
                          completed_at=now()
                    WHERE id=%s""",
                (approver, investigation_id),
            )
        return 200, {"ok": True, "investigation_id": investigation_id, "status": "dismissed"}
    except Exception as e:
        return 500, {"error": str(e)}


def delete_investigation(investigation_id: str) -> tuple[int, dict]:
    """Permanent removal — drops the row from postgres. Used by the History
    tab's X button to clear UI noise. Won't delete an in-flight investigation
    (status='running' or 'remediating') because doing so would orphan the
    spawned Job + leave it without a place to write its result."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}
    try:
        with DB.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM investigations WHERE id=%s AND status NOT IN ('running','remediating','awaiting_approval')",
                (investigation_id,),
            )
            if cur.rowcount == 0:
                # Either not found, or in a non-deletable state.
                return 409, {"error": "not deletable (in-flight or not found)"}
        return 200, {"ok": True, "investigation_id": investigation_id, "deleted": True}
    except Exception as e:
        return 500, {"error": str(e)}


def delete_all_investigations() -> tuple[int, dict]:
    """Bulk removal — drops every terminal investigation from postgres. Used by
    the History tab's 'Dismiss all' button. In-flight rows (running / remediating
    / awaiting_approval) are preserved so we never orphan a spawned Job or drop a
    pending approval. NOT recoverable."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}
    try:
        with DB.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM investigations WHERE status NOT IN ('running','remediating','awaiting_approval')",
            )
            deleted = cur.rowcount
        return 200, {"ok": True, "deleted": deleted}
    except Exception as e:
        return 500, {"error": str(e)}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=HTML_DIR, **kwargs)  # type: ignore[arg-type]

    def _json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorize_mutation(self) -> str | None:
        """Gate a mutating request. Returns the approver identity if allowed;
        otherwise writes the error response and returns None (caller must stop).

        Two independent checks:
        1. CSRF: require the custom `X-Requested-With` header. A cross-origin page
           cannot set it on a "simple" request, and setting it forces a CORS
           preflight this server never approves — so a browser CSRF (even one
           riding a valid oauth2-proxy session cookie) is blocked. Same-origin
           dashboard JS sets it explicitly.
        2. AuthN/AuthZ: a verified, in-group OIDC token (see verify_identity).
        """
        if not self.headers.get("X-Requested-With"):
            self._json(403, {"error": "missing X-Requested-With header (CSRF protection)"})
            return None
        if not DASHBOARD_AUTH_REQUIRED:
            # Explicit, deliberate opt-out (trusted network, SSO disabled).
            approver = (self.headers.get("X-Forwarded-Email")
                        or self.headers.get("X-Auth-Request-Email")
                        or "auth-disabled")
            print(f"WARNING: DASHBOARD_AUTH_REQUIRED=false — mutation allowed unauthenticated (approver={approver})", flush=True)
            return approver
        if not _AUTH_CONFIGURED:
            self._json(503, {"error": "dashboard auth is required but not configured "
                                      "(need OIDC_ISSUER_URL, OIDC_CLIENT_ID, and PyJWT); "
                                      "set DASHBOARD_AUTH_REQUIRED=false to override on a trusted network"})
            return None
        identity, reason = verify_identity(self.headers.get("Authorization", ""))
        if identity is None:
            self._json(401, {"error": f"unauthorized: {reason}"})
            return None
        return identity

    def do_GET(self) -> None:  # type: ignore[override]
        # Root → cluster-topology.html (no Apache-style directory listing).
        if self.path == "/" or self.path == "":
            self.path = "/cluster-topology.html"
            return super().do_GET()

        if self.path == "/data.json":
            with snapshot_lock:
                base = dict(snapshot)
            with metrics_lock:
                base.update(metrics)
            data = json.dumps(base, default=_json_default).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/investigations":
            return self._json(200, {"items": list_pending_investigations()})

        if self.path == "/investigations/all" or self.path.startswith("/investigations/all?"):
            return self._json(200, {"items": list_all_investigations(limit=20)})

        # /investigations/<uuid> — for post-approve polling
        m = re.match(r"^/investigations/([0-9a-f-]{36})$", self.path)
        if m:
            inv = get_investigation_detail(m.group(1))
            if not inv:
                return self._json(404, {"error": "not found"})
            return self._json(200, inv)

        super().do_GET()

    def do_POST(self) -> None:  # type: ignore[override]
        # Endpoints: /investigations/<id>/approve and /investigations/<id>/dismiss
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 3 and parts[0] == "investigations" and parts[2] in ("approve", "dismiss"):
            investigation_id = parts[1]
            try:
                uuid.UUID(investigation_id)
            except ValueError:
                return self._json(400, {"error": "invalid investigation id"})
            # Gate on a verified admin identity (approve spawns a cluster-mutating
            # Remediator Job). The verified token subject is the audit approver —
            # unspoofable, unlike the former X-Forwarded-For.
            approver = self._authorize_mutation()
            if approver is None:
                return  # error already written
            if parts[2] == "approve":
                status, body = approve_investigation(investigation_id, approver)
            else:
                status, body = dismiss_investigation(investigation_id, approver)
            return self._json(status, body)

        self.send_response(404); self.end_headers()

    def do_DELETE(self) -> None:  # type: ignore[override]
        # DELETE /investigations/<id> — permanently removes the row from postgres.
        # Used by the dashboard's History tab to clear noise. NOT recoverable.
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        # Deleting audit history is destructive and unrecoverable — same admin
        # gate as approve/dismiss.
        if parts[:1] == ["investigations"] and len(parts) in (1, 2):
            if self._authorize_mutation() is None:
                return  # error already written
        # DELETE /investigations — bulk "dismiss all" of terminal rows.
        if len(parts) == 1 and parts[0] == "investigations":
            status, body = delete_all_investigations()
            return self._json(status, body)
        if len(parts) == 2 and parts[0] == "investigations":
            investigation_id = parts[1]
            try:
                uuid.UUID(investigation_id)
            except ValueError:
                return self._json(400, {"error": "invalid investigation id"})
            status, body = delete_investigation(investigation_id)
            return self._json(status, body)
        self.send_response(404); self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # type: ignore[override]
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Allow concurrent requests so a slow approval doesn't block /data.json."""
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=metrics_loop, daemon=True).start()
    time.sleep(3)  # wait for first snapshot
    print(f"dashboard backend on :{PORT} (psycopg={'yes' if HAVE_PSYCOPG else 'no'}, db={DB_NAME})", flush=True)
    _ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
