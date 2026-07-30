#!/usr/bin/env python3
"""Event Watcher — the trigger engine.

Long-running Deployment. Responsibilities:

  1. Watch K8s API for actionable signals (Pod CrashLoopBackOff, OOMKilled,
     ImagePullBackOff, Node NotReady, FailedScheduling/FailedMount events).
  2. Debounce per (kind, namespace, name) using the postgres `debounce` table.
  3. Enforce concurrency cap and daily investigation budget.
  4. INSERT investigation row + spawn an Investigator Job.
  5. Reconcile per-namespace writer RoleBindings for `team-*` namespaces
     every 5 minutes.

Single replica. If killed mid-loop, the next start picks up live events;
historical events are not replayed (informers list-then-watch the current
state, which captures any persisting bad state at start time).

All env vars are documented in configmap.yaml + the README.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg
from datetime import datetime, timezone
from kubernetes import client, config, watch  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("event-watcher")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "unknown")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

WATCH_NAMESPACES = os.environ.get("WATCH_NAMESPACES", "*")
EXCLUDE_NAMESPACES = set(filter(None, os.environ.get("EXCLUDE_NAMESPACES", "").split(",")))

DEBOUNCE_WINDOW_SEC = int(os.environ.get("DEBOUNCE_WINDOW_SEC", "600"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_INVESTIGATIONS", "3"))
MAX_PER_DAY = int(os.environ.get("MAX_INVESTIGATIONS_PER_DAY", "50"))
# After we conclude a resource is out-of-scope (can't auto-remediate, e.g. a
# stuck AITeam in ai-platform), don't re-investigate it for this long. Without
# this, the stuck-resource scanner re-fires every DEBOUNCE_WINDOW_SEC forever,
# burning the daily budget on a verdict that won't change. Re-checks daily so a
# genuinely-changed resource is eventually picked up again.
OUT_OF_SCOPE_COOLDOWN_SEC = int(os.environ.get("OUT_OF_SCOPE_COOLDOWN_SEC", "86400"))

TRIGGERS = {
    "CrashLoopBackOff": os.environ.get("TRIGGER_CRASHLOOP", "true").lower() == "true",
    "OOMKilled":        os.environ.get("TRIGGER_OOMKILLED",  "true").lower() == "true",
    "ImagePullBackOff": os.environ.get("TRIGGER_IMAGEPULL",  "true").lower() == "true",
    "FailedScheduling": os.environ.get("TRIGGER_FAILEDSCHED","true").lower() == "true",
    "NodeNotReady":     os.environ.get("TRIGGER_NODENOTREADY","true").lower() == "true",
    "FailedMount":      os.environ.get("TRIGGER_FAILEDMOUNT", "true").lower() == "true",
    "StuckResource":    os.environ.get("TRIGGER_STUCKRESOURCE", "true").lower() == "true",
    # Missing Secret/ConfigMap referenced as an env (envFrom/valueFrom) → the
    # kubelet can't build the container config. Distinct from FailedMount, which
    # is a *volume* mount failure surfaced as a separate event.
    "CreateContainerConfigError": os.environ.get("TRIGGER_CONFIGERROR", "true").lower() == "true",
    # Container can't start (bad command/entrypoint, missing binary, perms).
    "RunContainerError": os.environ.get("TRIGGER_RUNCONTAINERERROR", "true").lower() == "true",
    # Node resource pressure (Memory/Disk/PID) or cordoned (unschedulable).
    "NodePressure": os.environ.get("TRIGGER_NODEPRESSURE", "true").lower() == "true",
    # Pod sandbox / CNI setup failure → pod stuck ContainerCreating.
    "FailedCreatePodSandBox": os.environ.get("TRIGGER_PODSANDBOX", "true").lower() == "true",
    # Pod Running but persistently not Ready (readiness probe failing past
    # POD_NOT_READY_THRESHOLD_SEC). Generous threshold avoids firing during
    # normal slow model loads (e.g. vLLM).
    "PodNotReady": os.environ.get("TRIGGER_PODNOTREADY", "true").lower() == "true",
}

# A Running pod that's still not Ready after this many seconds is treated as
# stuck (readiness probe persistently failing). Set high enough to clear normal
# container/model startup — vLLM model loads can take minutes.
POD_NOT_READY_THRESHOLD_SEC = int(os.environ.get("POD_NOT_READY_THRESHOLD_SEC", "600"))

# Threshold: a custom resource is considered "stuck" if it hasn't reached
# its healthy state within this many seconds since creation.
STUCK_RESOURCE_THRESHOLD_SEC = int(os.environ.get("STUCK_RESOURCE_THRESHOLD_SEC", "600"))
# How often to scan for stuck resources.
STUCK_RESOURCE_POLL_INTERVAL = int(os.environ.get("STUCK_RESOURCE_POLL_INTERVAL", "60"))

KIRO_MODEL_INVESTIGATE = os.environ.get("KIRO_MODEL_INVESTIGATE", "auto")
PYTHON_IMAGE = os.environ.get("PYTHON_IMAGE", "python:3.12-slim")
KUBECTL_VERSION = os.environ.get("KUBECTL_VERSION", "v1.32.5")
NAMESPACE = "ai-platform"  # where the watcher runs and Investigator/Remediator Jobs are spawned

# PHA is "enabled" only when a Kiro API key is present. The
# platform-health-agent-secrets Secret (mounted here, optional) is created
# manually with kubectl (see PLATFORM-HEALTH-AGENT.md). Without it the watcher
# idles gracefully — it boots, serves a healthy endpoint, watches nothing and
# spawns no Jobs — so the always-synced cluster-dashboard app stays Healthy on
# clusters that never opted in. See main().
KIRO_API_KEY = os.environ.get("KIRO_API_KEY", "")
PHA_ENABLED = bool(KIRO_API_KEY.strip())

# Marker label on PHA's own workloads (event-watcher Deployment + the
# Investigator/Remediator Jobs it spawns). Now that they share the ai-platform
# namespace with the platform services we DO watch (litellm, langfuse, …), the
# watcher must skip its own pods/Jobs to avoid investigating itself.
PHA_PART_OF = "platform-health-agent"
# Events carry no labels — fall back to PHA's deterministic resource names.
_OWN_NAME_RE = re.compile(r"^(investigator-[0-9a-f]{8}|remediator-[0-9a-f]{8}|event-watcher-)")

ALLOWED_REMEDIATION_NAMESPACES_RE = re.compile(r"^(inference|team-.+)$")


def _is_own_workload(namespace: str | None, name: str | None, labels: dict | None) -> bool:
    """True if the resource is one of PHA's own pods/Jobs (event-watcher or a
    spawned Investigator/Remediator). Prevents self-investigation now that PHA
    shares the ai-platform namespace with the workloads it watches.

    Pods carry labels (reliable); Events don't, so we fall back to the
    deterministic name prefixes for PHA's own resources."""
    if namespace != NAMESPACE:
        return False
    if labels and labels.get("app.kubernetes.io/part-of") == PHA_PART_OF:
        return True
    return bool(_OWN_NAME_RE.match(name or ""))

# ─── ReplicaSet name pattern: <deployment-name>-<6-10 hex hash> ────────────
_RS_HASH_RE = re.compile(r"-[a-f0-9]{6,10}$")


def _owner_root(pod) -> tuple[str, str]:
    """Walk pod -> ReplicaSet -> Deployment to find the top-level controller.

    Used as the debounce/dedup key so all pods of the same Deployment
    (different replica hashes) share one investigation.
    Falls back to (Pod, pod_name) for orphan pods.
    """
    refs = pod.metadata.owner_references or []
    for ref in refs:
        if ref.kind == "ReplicaSet":
            # Most ReplicaSets are owned by a Deployment; the RS name is
            # <deployment-name>-<hash>. Strip the hash.
            dep_name = _RS_HASH_RE.sub("", ref.name)
            return ("Deployment", dep_name)
        if ref.kind in ("StatefulSet", "DaemonSet", "Job"):
            return (ref.kind, ref.name)
    return ("Pod", pod.metadata.name)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db_connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        autocommit=True,
        connect_timeout=10,
    )


def db_with_retry() -> psycopg.Connection:
    """Reconnect with exponential backoff."""
    backoff = 1
    while True:
        try:
            conn = db_connect()
            log.info("postgres connected")
            return conn
        except Exception as e:
            log.warning("postgres connect failed: %s — retry in %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Debounce / capacity checks
# ---------------------------------------------------------------------------

def should_throttle(conn: psycopg.Connection, kind: str, namespace: str, name: str) -> str | None:
    """Return reason string if event should be skipped, else None."""
    with conn.cursor() as cur:
        # 0. In-flight dedup: refuse to spawn a duplicate for the same resource.
        # This guards against debounce expiry or restart-induced gaps.
        cur.execute(
            """SELECT id FROM investigations
                WHERE resource_kind=%s AND resource_namespace=%s AND resource_name=%s
                  AND status IN ('running','awaiting_approval','remediating')
                LIMIT 1""",
            (kind, namespace, name),
        )
        active = cur.fetchone()
        if active:
            return f"already_in_flight({active[0]})"

        # 0b. Out-of-scope cooldown: if we recently concluded this resource is
        # out-of-scope (can't auto-remediate), don't keep re-investigating it —
        # the verdict won't change and it would drain the daily budget.
        cur.execute(
            """SELECT 1 FROM investigations
                WHERE resource_kind=%s AND resource_namespace=%s AND resource_name=%s
                  AND out_of_scope = true
                  AND created_at > now() - make_interval(secs => %s)
                LIMIT 1""",
            (kind, namespace, name, OUT_OF_SCOPE_COOLDOWN_SEC),
        )
        if cur.fetchone():
            return "out_of_scope_cooldown"

        # 1. Per-resource debounce.
        cur.execute(
            "SELECT last_seen FROM debounce WHERE resource_kind=%s AND resource_namespace=%s AND resource_name=%s",
            (kind, namespace, name),
        )
        row = cur.fetchone()
        if row:
            last_seen: datetime = row[0]
            if (datetime.now(timezone.utc) - last_seen).total_seconds() < DEBOUNCE_WINDOW_SEC:
                return "debounce"

        # 2. Concurrency cap.
        cur.execute("SELECT n FROM active_investigation_count")
        active = cur.fetchone()[0] or 0  # type: ignore[index]
        if active >= MAX_CONCURRENT:
            return f"concurrency_cap({active}/{MAX_CONCURRENT})"

        # 3. Daily budget.
        cur.execute("SELECT investigations FROM today_counters")
        today = cur.fetchone()[0] or 0  # type: ignore[index]
        if today >= MAX_PER_DAY:
            return f"daily_budget({today}/{MAX_PER_DAY})"

    return None


def record_trigger(conn: psycopg.Connection, kind: str, namespace: str, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO debounce (resource_kind, resource_namespace, resource_name, last_seen)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (resource_kind, resource_namespace, resource_name)
               DO UPDATE SET last_seen = EXCLUDED.last_seen""",
            (kind, namespace, name),
        )
        cur.execute(
            """INSERT INTO daily_counters (day, investigations) VALUES (CURRENT_DATE, 1)
               ON CONFLICT (day) DO UPDATE SET investigations = daily_counters.investigations + 1""",
        )


# ---------------------------------------------------------------------------
# Investigator Job spec
# ---------------------------------------------------------------------------

def build_investigator_job(investigation_id: str, event_payload: dict) -> dict:
    """Construct the batch/v1 Job for an Investigator run.

    Returns a dict ready to pass to BatchV1Api.create_namespaced_job(body=…).

    Design notes:
    - serviceAccountName: platform-health-agent-reader → cluster-wide read only.
    - Two initContainers:
        a) install-tools  → downloads kubectl + kiro-cli into /tools (emptyDir)
        b) install-pydeps → pip-installs psycopg into /pydeps (emptyDir)
      Why two? They run in parallel via initContainers? No — initContainers
      run sequentially. But each is small and fast (~10–15s combined). Trading
      a bit of cold-start time for zero custom image ops.
    - main container: python:3.12-slim, runs investigate.sh.
    - activeDeadlineSeconds: 600 (10 min hard ceiling).
    - ttlSecondsAfterFinished: 3600 (Job + Pod garbage-collected after 1h).
    """
    job_name = f"investigator-{investigation_id[:8]}"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "platform-health-agent-investigator",
                "app.kubernetes.io/part-of": "platform-health-agent",
                "investigation-id": investigation_id,
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": 3600,
            "activeDeadlineSeconds": 600,
            "backoffLimit": 0,                  # don't retry — surface the failure in the dashboard
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "platform-health-agent-investigator",
                        "investigation-id": investigation_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "platform-health-agent-reader",
                    "automountServiceAccountToken": True,
                    "nodeSelector": {"kubernetes.io/arch": "amd64"},
                    "initContainers": _bootstrap_init_containers(),
                    "containers": [{
                        "name": "investigator",
                        "image": PYTHON_IMAGE,
                        "command": ["/bin/sh", "/scripts/investigate.sh"],
                        "env": _agent_env(investigation_id, event_payload),
                        "volumeMounts": _agent_volume_mounts(),
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
                    "volumes": _agent_volumes(),
                },
            },
        },
    }


def _bootstrap_init_containers() -> list[dict]:
    return [
        {
            "name": "install-tools",
            "image": "alpine:3.20",
            "command": ["/bin/sh", "-c"],
            "args": [
                f"set -eu; cd /tools; "
                f"echo 'fetching kubectl {KUBECTL_VERSION}…'; "
                f"wget -qO kubectl https://dl.k8s.io/release/{KUBECTL_VERSION}/bin/linux/amd64/kubectl && chmod +x kubectl; "
                f"echo 'fetching kiro-cli installer…'; "
                f"apk add -q curl bash; "
                f"export HOME=/tools; "
                f"curl -fsSL https://cli.kiro.dev/install | bash; "
                # The installer drops THREE binaries (kiro-cli, kiro-cli-chat,
                # kiro-cli-term) at $HOME/.local/bin/. The launcher (kiro-cli)
                # forks to kiro-cli-chat which must also be on PATH.
                f"mv /tools/.local/bin/* /tools/ 2>/dev/null || true; "
                f"chmod +x /tools/kiro-cli /tools/kiro-cli-chat /tools/kiro-cli-term 2>/dev/null || true; "
                f"ls -la /tools/",
            ],
            "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
            "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        },
        {
            "name": "install-pydeps",
            "image": PYTHON_IMAGE,
            "command": ["/bin/sh", "-c"],
            "args": [
                "set -eu; "
                # psycopg for the wrapper's DB writes; awslabs.eks-mcp-server
                # for kiro-cli's MCP tool surface (replaces raw kubectl).
                "pip install --no-cache-dir --target=/pydeps "
                "  psycopg[binary]==3.2.3 "
                "  awslabs.eks-mcp-server",
            ],
            "volumeMounts": [{"name": "pydeps", "mountPath": "/pydeps"}],
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        },
    ]


def _agent_env(investigation_id: str, event_payload: dict) -> list[dict]:
    """Env vars common to Investigator and Remediator containers."""
    return [
        {"name": "INVESTIGATION_ID", "value": investigation_id},
        {"name": "EVENT_PAYLOAD", "value": json.dumps(event_payload)},
        {"name": "PATH", "value": "/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        {"name": "PYTHONPATH", "value": "/pydeps"},
        {"name": "HOME", "value": "/tmp"},
        # From config ConfigMap.
        *[{"name": k, "valueFrom": {"configMapKeyRef": {"name": "platform-health-agent-config", "key": k}}}
          for k in ["CLUSTER_NAME", "AWS_REGION", "DB_HOST", "DB_PORT", "DB_NAME",
                    "KIRO_MODEL_INVESTIGATE", "KIRO_MODEL_REMEDIATE"]],
        # DB credentials reuse the existing platform Postgres credentials.
        {"name": "DB_USER",      "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "username"}}},
        {"name": "DB_PASSWORD",  "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "password"}}},
        # Only secret needed: kiro-cli API key. Approvals UX is in the cluster-dashboard.
        {"name": "KIRO_API_KEY", "valueFrom": {"secretKeyRef": {"name": "platform-health-agent-secrets",   "key": "KIRO_API_KEY"}}},
    ]


def _agent_volume_mounts() -> list[dict]:
    return [
        {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
        {"name": "tools",   "mountPath": "/tools",   "readOnly": True},
        {"name": "pydeps",  "mountPath": "/pydeps",  "readOnly": True},
        {"name": "results", "mountPath": "/results"},
        {"name": "tmp",     "mountPath": "/tmp"},
    ]


def _agent_volumes() -> list[dict]:
    return [
        {"name": "scripts", "configMap": {"name": "platform-health-agent-scripts", "defaultMode": 0o755}},
        {"name": "tools",   "emptyDir": {}},
        {"name": "pydeps",  "emptyDir": {}},
        {"name": "results", "emptyDir": {}},
        {"name": "tmp",     "emptyDir": {}},
    ]


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

def is_namespace_watched(ns: str) -> bool:
    if ns in EXCLUDE_NAMESPACES:
        return False
    if WATCH_NAMESPACES == "*":
        return True
    return ns in WATCH_NAMESPACES.split(",")


def detect_pod_trigger(pod) -> tuple[str, dict] | None:
    """Inspect a Pod object; return (trigger_kind, payload) if actionable, else None.

    Checks BOTH initContainerStatuses and containerStatuses — init container
    failures (exit code != 0 with retries) manifest as Pending pods with
    initContainerStatuses[*].state.waiting.reason == 'CrashLoopBackOff' or
    state.terminated.reason == 'Error' with restartCount > 3.
    """
    if pod.metadata.namespace and not is_namespace_watched(pod.metadata.namespace):
        return None
    # Skip PHA's own pods (event-watcher + spawned Investigator/Remediator Jobs)
    # so the agent never investigates itself now that it lives in ai-platform.
    if _is_own_workload(pod.metadata.namespace, pod.metadata.name, pod.metadata.labels):
        return None
    if not pod.status:
        return None

    init_cs_list = pod.status.init_container_statuses or []
    main_cs_list = pod.status.container_statuses or []

    # Iterate init containers first — if an init container is failing, the
    # main containers will be PodInitializing (which is NOT a trigger).
    for cs_list, kind_label in ((init_cs_list, "init"), (main_cs_list, "main")):
        for cs in cs_list:
            # OOMKilled (most recent termination)
            last = cs.last_state.terminated if cs.last_state else None
            if last and last.reason == "OOMKilled" and TRIGGERS["OOMKilled"]:
                return "OOMKilled", _pod_payload(pod, container=f"{cs.name} ({kind_label})", detail=str(last))
            # Init containers: also check current-state.terminated for non-zero exits
            # with high restart count (kubelet retries init containers but state may be
            # terminated rather than waiting/CrashLoopBackOff between retries).
            if kind_label == "init":
                term = cs.state.terminated if cs.state else None
                if (term and term.exit_code != 0 and (cs.restart_count or 0) > 3
                        and TRIGGERS["CrashLoopBackOff"]):
                    return "CrashLoopBackOff", _pod_payload(
                        pod, container=f"{cs.name} (init)",
                        detail=f"init container exit {term.exit_code} (reason={term.reason}); restarts={cs.restart_count}",
                    )
            # Common: state.waiting checks (CrashLoopBackOff / ImagePullBackOff)
            waiting = cs.state.waiting if cs.state else None
            if (waiting and waiting.reason == "CrashLoopBackOff"
                    and (cs.restart_count or 0) > 3 and TRIGGERS["CrashLoopBackOff"]):
                return "CrashLoopBackOff", _pod_payload(
                    pod, container=f"{cs.name} ({kind_label})",
                    detail=waiting.message or "",
                )
            if (waiting and waiting.reason in ("ImagePullBackOff", "ErrImagePull")
                    and TRIGGERS["ImagePullBackOff"]):
                return "ImagePullBackOff", _pod_payload(
                    pod, container=f"{cs.name} ({kind_label})",
                    detail=waiting.message or "",
                )
            # Missing Secret/ConfigMap referenced via envFrom/valueFrom → the
            # container config can't be built. No restart-count gate: this state
            # is persistent (kubelet retries forever) and actionable immediately.
            if (waiting and waiting.reason in ("CreateContainerConfigError", "CreateContainerError")
                    and TRIGGERS["CreateContainerConfigError"]):
                return "CreateContainerConfigError", _pod_payload(
                    pod, container=f"{cs.name} ({kind_label})",
                    detail=waiting.message or "",
                )
            # Container can't start: bad command/entrypoint, missing binary,
            # permission error. Persistent state, actionable immediately.
            if (waiting and waiting.reason in ("RunContainerError", "StartError")
                    and TRIGGERS["RunContainerError"]):
                return "RunContainerError", _pod_payload(
                    pod, container=f"{cs.name} ({kind_label})",
                    detail=waiting.message or "",
                )

    # Pod Running but persistently not Ready (readiness probe failing). No
    # waiting/terminated state to key on — gate on age so we don't fire during
    # normal startup. Checked after the per-container loop so a container that's
    # actively CrashLooping/erroring (handled above) takes precedence.
    if TRIGGERS["PodNotReady"] and _pod_persistently_unready(pod):
        return "PodNotReady", _pod_payload(
            pod, container="(pod)",
            detail=f"Pod Running but not Ready for >{POD_NOT_READY_THRESHOLD_SEC}s "
                   f"(readiness probe failing)",
        )
    return None


def _pod_persistently_unready(pod) -> bool:
    """True if the pod is Running but has been not-Ready longer than the
    threshold — i.e. a readiness probe that won't pass, not a slow start."""
    if not pod.status or pod.status.phase != "Running":
        return False
    # All containers must be started (else it's a startup/init issue handled
    # elsewhere); the symptom is specifically Ready=False while Running.
    conds = {c.type: c for c in (pod.status.conditions or [])}
    ready = conds.get("Ready")
    if not ready or ready.status != "False":
        return False
    # Age since the Ready condition last flipped (fall back to pod start time).
    ref_ts = ready.last_transition_time or pod.status.start_time
    if not ref_ts:
        return False
    return (datetime.now(timezone.utc) - ref_ts).total_seconds() >= POD_NOT_READY_THRESHOLD_SEC


def _pod_payload(pod, container: str, detail: str) -> dict:
    """Build the trigger payload.

    kind/name point at the OWNER (Deployment/StatefulSet/etc.), not the Pod.
    This is the debounce/dedup key — all pods of the same Deployment share
    one investigation. The actual pod name + container are kept as context
    so the investigator can read the right logs.
    """
    owner_kind, owner_name = _owner_root(pod)
    return {
        "kind": owner_kind,
        "namespace": pod.metadata.namespace,
        "name": owner_name,
        "pod_name": pod.metadata.name,
        "node": pod.spec.node_name,
        "container": container,
        "detail": detail,
        "owner_references": [
            {"kind": o.kind, "name": o.name} for o in (pod.metadata.owner_references or [])
        ],
        "phase": pod.status.phase if pod.status else None,
    }


def detect_event_trigger(event) -> tuple[str, dict] | None:
    """Inspect a v1.Event for FailedScheduling / FailedMount."""
    if event.type != "Warning":
        return None
    obj = event.involved_object
    if obj.namespace and not is_namespace_watched(obj.namespace):
        return None
    # Skip events about PHA's own workloads (no labels on events → name match).
    if _is_own_workload(obj.namespace, obj.name, None):
        return None
    payload = {
        "kind": obj.kind,
        "namespace": obj.namespace or "",
        "name": obj.name,
        "reason": event.reason,
        "message": event.message,
        "count": event.count,
    }
    if event.reason == "FailedScheduling" and TRIGGERS["FailedScheduling"]:
        return "FailedScheduling", payload
    if event.reason == "FailedMount" and TRIGGERS["FailedMount"]:
        return "FailedMount", payload
    # Pod sandbox / CNI setup failure → pod stuck ContainerCreating.
    if event.reason in ("FailedCreatePodSandBox", "FailedPodSandBoxStatus") \
            and TRIGGERS["FailedCreatePodSandBox"]:
        return "FailedCreatePodSandBox", payload
    return None


def detect_node_trigger(node) -> tuple[str, dict] | None:
    if not node.status or not node.status.conditions:
        return None

    def _node_payload(trigger_reason: str, message: str) -> dict:
        return {
            "kind": "Node",
            "namespace": "",
            "name": node.metadata.name,
            "reason": trigger_reason,
            "message": message,
        }

    conds = {c.type: c for c in node.status.conditions}

    # NotReady takes precedence — it's the most severe node signal.
    ready = conds.get("Ready")
    if TRIGGERS["NodeNotReady"] and ready and ready.status == "False":
        return "NodeNotReady", _node_payload(ready.reason, ready.message)

    if TRIGGERS["NodePressure"]:
        # Resource pressure: condition present AND True.
        for ctype in ("MemoryPressure", "DiskPressure", "PIDPressure"):
            c = conds.get(ctype)
            if c and c.status == "True":
                return "NodePressure", _node_payload(ctype, c.message or ctype)
        # Cordoned: spec.unschedulable. Skip Karpenter's brief
        # consolidation/drain churn by ignoring nodes already being deleted.
        spec = node.spec
        if spec and getattr(spec, "unschedulable", False) \
                and not (node.metadata.deletion_timestamp):
            return "NodePressure", _node_payload(
                "Unschedulable", f"Node {node.metadata.name} is cordoned (unschedulable)")
    return None


# ---------------------------------------------------------------------------
# Spawn helpers
# ---------------------------------------------------------------------------

def spawn_investigation(conn: psycopg.Connection, batch_v1: client.BatchV1Api,
                        trigger_kind: str, payload: dict) -> None:
    namespace = payload.get("namespace", "")
    name = payload["name"]
    resource_kind = payload["kind"]

    skip = should_throttle(conn, resource_kind, namespace, name)
    if skip:
        log.info("skip %s/%s/%s — %s", resource_kind, namespace or "-", name, skip)
        return

    investigation_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO investigations
                  (id, status, trigger_kind, resource_kind, resource_namespace, resource_name, event_payload)
               VALUES (%s, 'pending', %s, %s, %s, %s, %s)""",
            (investigation_id, trigger_kind, resource_kind, namespace, name, json.dumps(payload)),
        )

    job = build_investigator_job(investigation_id, {"trigger_kind": trigger_kind, **payload})
    try:
        batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job)
    except ApiException as e:
        log.error("Job create failed for %s: %s", investigation_id, e)
        with conn.cursor() as cur:
            cur.execute("UPDATE investigations SET status='failed', error_message=%s WHERE id=%s",
                        (f"Job create failed: {e}", investigation_id))
        return

    record_trigger(conn, resource_kind, namespace, name)
    with conn.cursor() as cur:
        cur.execute("UPDATE investigations SET status='running' WHERE id=%s", (investigation_id,))
    log.info("spawned investigator %s for %s/%s/%s (kind=%s)",
             investigation_id, resource_kind, namespace or "-", name, trigger_kind)


# ---------------------------------------------------------------------------
# Watcher loops (one thread each)
# ---------------------------------------------------------------------------

stop_event = threading.Event()


def _stop(*_):
    log.info("shutdown requested")
    stop_event.set()


def _db_alive(conn) -> bool:
    """True if the connection is usable. psycopg exposes `closed`; `broken` is
    set after a failed operation."""
    return not conn.closed and not getattr(conn, "broken", False)


def _watch_loop(label, conn_factory, lister, detect) -> None:
    """Generic K8s watch loop, shared by the pod/event/node watchers.

    Opens ONE postgres connection and REUSES it across the ~5-minute watch-stream
    restarts (the stream returns every timeout_seconds and on transient errors).
    The previous code re-opened `conn_factory()` on every loop iteration and never
    closed the old connection, leaking ~1 connection per 5 minutes per thread and
    eventually exhausting the shared platform Postgres. We reconnect ONLY when the
    connection has actually broken, and always close it on shutdown.

    Args:
        label: short name for log lines ("pod"/"event"/"node").
        conn_factory: returns a live psycopg connection (db_with_retry).
        lister: given a CoreV1Api, returns the list_* method to stream.
        detect: given the streamed object, returns a trigger tuple or None.
    """
    conn = conn_factory()
    batch_v1 = client.BatchV1Api()
    try:
        while not stop_event.is_set():
            try:
                if not _db_alive(conn):
                    conn = conn_factory()
                v1 = client.CoreV1Api()
                w = watch.Watch()
                for ev in w.stream(lister(v1), timeout_seconds=300):
                    if stop_event.is_set():
                        w.stop(); break
                    trig = detect(ev["object"])
                    if trig:
                        spawn_investigation(conn, batch_v1, trig[0], trig[1])
            except Exception as e:
                log.warning("%s watch error: %s — restarting", label, e)
                # Reconnect only if the DB connection is the casualty; a K8s
                # stream error leaves the connection healthy and reusable.
                if not _db_alive(conn):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = conn_factory()
                time.sleep(5)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def watch_pods(conn_factory) -> None:
    _watch_loop("pod", conn_factory, lambda v1: v1.list_pod_for_all_namespaces, detect_pod_trigger)


def watch_events(conn_factory) -> None:
    _watch_loop("event", conn_factory, lambda v1: v1.list_event_for_all_namespaces, detect_event_trigger)


def watch_nodes(conn_factory) -> None:
    _watch_loop("node", conn_factory, lambda v1: v1.list_node, detect_node_trigger)


# ---------------------------------------------------------------------------
# Namespace reconciler — keeps writer RoleBindings in sync with team-* ns.
# ---------------------------------------------------------------------------

def reconcile_rolebindings_loop() -> None:
    """Every 5 min: ensure a writer RoleBinding exists in every `team-*`
    namespace and the `inference` namespace."""
    while not stop_event.is_set():
        try:
            v1 = client.CoreV1Api()
            rbac = client.RbacAuthorizationV1Api()

            namespaces = [n.metadata.name for n in v1.list_namespace().items]
            allowed = [n for n in namespaces if ALLOWED_REMEDIATION_NAMESPACES_RE.match(n)]
            log.info("reconciling writer RoleBindings for: %s", ",".join(allowed))

            for ns in allowed:
                try:
                    rbac.read_namespaced_role_binding(name="platform-health-agent-writer", namespace=ns)
                    continue
                except ApiException as e:
                    if e.status != 404:
                        raise
                rb = client.V1RoleBinding(
                    metadata=client.V1ObjectMeta(
                        name="platform-health-agent-writer", namespace=ns,
                        labels={"app.kubernetes.io/managed-by": "platform-health-agent-reconciler",
                                "app.kubernetes.io/part-of": "platform-health-agent"},
                    ),
                    role_ref=client.V1RoleRef(
                        api_group="rbac.authorization.k8s.io",
                        kind="ClusterRole", name="platform-health-agent-writer"),
                    # Plain dict for subjects: kubernetes-client renamed
                    # V1Subject → RbacV1Subject in v28+. Dict form is
                    # accepted by the API and version-stable.
                    subjects=[{
                        "kind": "ServiceAccount",
                        "name": "platform-health-agent-writer",
                        "namespace": NAMESPACE,
                    }],
                )
                rbac.create_namespaced_role_binding(namespace=ns, body=rb)
                log.info("created writer RoleBinding in %s", ns)

            # Sweep stale bindings: any RoleBinding labeled by us in a namespace
            # that no longer matches the pattern → delete.
            try:
                stale = rbac.list_role_binding_for_all_namespaces(
                    label_selector="app.kubernetes.io/managed-by=platform-health-agent-reconciler",
                ).items
            except ApiException:
                stale = []
            for rb in stale:
                if rb.metadata.namespace not in allowed:
                    rbac.delete_namespaced_role_binding(
                        name=rb.metadata.name, namespace=rb.metadata.namespace)
                    log.info("removed stale writer RoleBinding from %s", rb.metadata.namespace)

        except Exception as e:
            log.warning("reconciler error: %s", e)

        for _ in range(300):
            if stop_event.is_set(): return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Stuck custom-resource watcher — periodic poll, fires when KRO CRs
# haven't reached their healthy state within STUCK_RESOURCE_THRESHOLD_SEC.
# Catches the gap that pod-level triggers miss: KRO reconcile errors,
# an endpoint stuck deploying, an AITeam stuck onboarding, etc.
# ---------------------------------------------------------------------------

def _resource_age_seconds(obj: dict) -> float:
    ts = obj.get("metadata", {}).get("creationTimestamp", "")
    if not ts:
        return 0.0
    try:
        created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds()
    except Exception:
        return 0.0


def _is_stuck_aiteam(team: dict) -> bool:
    status = team.get("status") or {}
    # KRO AITeam reports `state: ACTIVE` plus a Ready condition when healthy.
    # The older heuristic (`phase` / `ready` boolean) does not match KRO's
    # actual schema and produced false positives on every healthy team.
    if status.get("state") == "ACTIVE":
        return False
    for c in (status.get("conditions") or []):
        if c.get("type") == "Ready" and c.get("status") == "True":
            return False
    return _resource_age_seconds(team) >= STUCK_RESOURCE_THRESHOLD_SEC


def _is_stuck_vllmendpoint(ep: dict) -> bool:
    # VLLMEndpoint (simple vLLM) is healthy once its Deployment is available,
    # which the RGD surfaces as status.ready == "True".
    status = ep.get("status") or {}
    if str(status.get("ready", "False")) == "True":
        return False
    return _resource_age_seconds(ep) >= STUCK_RESOURCE_THRESHOLD_SEC


def _is_stuck_llmdendpoint(ep: dict) -> bool:
    # LLMDEndpoint (llm-d scale tier) is healthy when the vLLM replicas are
    # ready AND the router (EPP + InferencePool, delivered as an ArgoCD
    # Application) reports Healthy via status.routerHealth.
    status = ep.get("status") or {}
    ready = str(status.get("ready", "False")) == "True"
    router_healthy = status.get("routerHealth", "") == "Healthy"
    if ready and router_healthy:
        return False
    return _resource_age_seconds(ep) >= STUCK_RESOURCE_THRESHOLD_SEC


def watch_stuck_resources(conn_factory) -> None:
    """Periodically scan KRO custom resources; fire StuckResource trigger
    on those that haven't reached healthy state in time.

    Resources scanned (read-only):
      - vllmendpoints.kro.run             (in `inference` ns; simple vLLM)
      - llmdendpoints.kro.run             (in `inference` ns; llm-d scale tier)
      - llmddisaggendpoints.kro.run       (in `inference` ns; llm-d P/D disaggregated)
      - aiteams.kro.run                   (cluster-wide, in `ai-platform` ns)
    """
    if not TRIGGERS["StuckResource"]:
        log.info("StuckResource trigger disabled — watcher exiting")
        return
    custom = client.CustomObjectsApi()
    batch_v1 = client.BatchV1Api()

    while not stop_event.is_set():
        try:
            conn = conn_factory()

            # --- VLLMEndpoints (simple vLLM) + LLMDEndpoints (llm-d scale) ---
            for plural, kind, stuck_fn in (
                ("vllmendpoints", "VLLMEndpoint", _is_stuck_vllmendpoint),
                ("llmdendpoints", "LLMDEndpoint", _is_stuck_llmdendpoint),
                ("llmddisaggendpoints", "LLMDDisaggEndpoint", _is_stuck_llmdendpoint),
            ):
                try:
                    items = custom.list_namespaced_custom_object(
                        group="kro.run", version="v1alpha1",
                        namespace="inference", plural=plural,
                    ).get("items", [])
                except ApiException as e:
                    if e.status != 404:
                        log.warning("list %s: %s", plural, e)
                    items = []
                for ep in items:
                    if not stuck_fn(ep):
                        continue
                    st = ep.get("status") or {}
                    payload = {
                        "kind": kind,
                        "namespace": ep["metadata"].get("namespace", ""),
                        "name": ep["metadata"]["name"],
                        "ready": str(st.get("ready", "False")),
                        "routerHealth": st.get("routerHealth", ""),
                        "availableReplicas": st.get("availableReplicas", 0),
                        "model": (ep.get("spec") or {}).get("model", ""),
                        "age_seconds": int(_resource_age_seconds(ep)),
                    }
                    spawn_investigation(conn, batch_v1, "StuckResource", payload)

            # --- AITeams ---
            try:
                teams = custom.list_namespaced_custom_object(
                    group="kro.run", version="v1alpha1",
                    namespace="ai-platform", plural="aiteams",
                ).get("items", [])
            except ApiException as e:
                if e.status != 404:
                    log.warning("list aiteams: %s", e)
                teams = []
            for team in teams:
                if not _is_stuck_aiteam(team):
                    continue
                payload = {
                    "kind": "AITeam",
                    "namespace": team["metadata"].get("namespace", ""),
                    "name": team["metadata"]["name"],
                    "phase": (team.get("status") or {}).get("phase", "Pending"),
                    "message": (team.get("status") or {}).get("message", ""),
                    "age_seconds": int(_resource_age_seconds(team)),
                }
                spawn_investigation(conn, batch_v1, "StuckResource", payload)

        except Exception as e:
            log.warning("stuck-resource scan error: %s", e)

        for _ in range(STUCK_RESOURCE_POLL_INTERVAL):
            if stop_event.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Health / liveness HTTP server
# ---------------------------------------------------------------------------

def _health_server() -> None:
    """Tiny HTTP server on :8080 → 200 if K8s API + DB are reachable."""
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                client.CoreV1Api().list_namespace(limit=1)
                with db_connect() as c, c.cursor() as cur:
                    cur.execute("SELECT 1")
                self.send_response(200); self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                self.send_response(503); self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, *_): pass

    http.server.HTTPServer(("", 8080), H).serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    config.load_incluster_config()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Health server starts unconditionally so the Deployment is Ready/Healthy
    # even when PHA is disabled — that's what keeps the always-synced
    # cluster-dashboard app green on clusters that never opted in.
    threading.Thread(target=_health_server, daemon=True).start()

    # Graceful idle: no Kiro API key → the agent is disabled. Don't watch, don't
    # spawn Jobs (the spawned Jobs require KIRO_API_KEY and would fail anyway).
    # Just idle until SIGTERM. Enable by creating the platform-health-agent-secrets
    # Secret (kubectl) and restarting this Deployment.
    if not PHA_ENABLED:
        log.warning("Platform Health Agent DISABLED — no KIRO_API_KEY present. "
                    "Watcher idling (no investigations). To enable: create the "
                    "platform-health-agent-secrets Secret in ai-platform with a "
                    "KIRO_API_KEY, then restart this deployment "
                    "(kubectl rollout restart deployment event-watcher -n ai-platform).")
        while not stop_event.is_set():
            time.sleep(1)
        return 0

    threads = [
        threading.Thread(target=watch_pods,            args=(db_with_retry,), daemon=True, name="pods"),
        threading.Thread(target=watch_events,          args=(db_with_retry,), daemon=True, name="events"),
        threading.Thread(target=watch_nodes,           args=(db_with_retry,), daemon=True, name="nodes"),
        threading.Thread(target=watch_stuck_resources, args=(db_with_retry,), daemon=True, name="stuck-resources"),
        threading.Thread(target=reconcile_rolebindings_loop,                  daemon=True, name="reconciler"),
    ]
    for t in threads:
        t.start()
    log.info("event-watcher started: cluster=%s region=%s triggers=%s",
             CLUSTER_NAME, AWS_REGION,
             ",".join(k for k, v in TRIGGERS.items() if v))

    while not stop_event.is_set():
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
