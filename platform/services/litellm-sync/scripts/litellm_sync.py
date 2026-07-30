#!/usr/bin/env python3
"""litellm-sync — the single owner of LiteLLM's model registry.

Watches the three serving-tier custom resources cluster-wide and keeps LiteLLM's
model list in sync, so the master key never has to enter a workload namespace and
models can be deployed into any namespace (e.g. per-team `team-*` namespaces):

  Tiers (kro.run/v1alpha1), each in ANY namespace:
    - vllmendpoints        -> api_base http://<name>-vllm.<ns>.svc.cluster.local:8000/v1
    - llmdendpoints        -> api_base http://<name>-epp.<ns>.svc.cluster.local:80/v1
    - llmddisaggendpoints  -> api_base http://<name>-epp.<ns>.svc.cluster.local:80/v1

Lifecycle (finalizer-driven, self-healing):

  1. WATCH each kind cluster-wide.
  2. Live object WITHOUT our finalizer      -> PATCH to add it, then REGISTER the
     model with LiteLLM (POST /model/new). LiteLLM alias = the CR name; upstream
     served-model-name = spec.model.
  3. Object WITH deletionTimestamp          -> DEREGISTER the model, then PATCH to
     remove our finalizer so deletion can complete.
  4. RECONCILE every RECONCILE_INTERVAL_SEC -> re-add finalizers + re-register any
     model missing from the live router (repairs drift after a LiteLLM restart,
     which reloads only the static config), and deregister orphaned DB-registered
     models whose CR no longer exists.

Registration is idempotent: a model already in the live /v1/models is left alone;
otherwise a stale DB entry is removed and the model re-added (so it lands back in
the running router). Deregistration only ever touches DB-registered models
(model_info.db_model == True) — static config models (e.g. the Bedrock
claude-opus-4-8 in litellm.yaml) have db_model == False and are never deleted.

Single replica, no database. If killed mid-loop, the next start re-lists current
state (watch is list-then-watch) and the reconcile loop repairs any drift. All
config is via env vars (see deployment.yaml).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from kubernetes import client, config, watch  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("litellm-sync")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LITELLM_BASE_URL = os.environ.get(
    "LITELLM_BASE_URL", "http://litellm.ai-platform.svc.cluster.local:4000"
).rstrip("/")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
FINALIZER = os.environ.get("FINALIZER", "litellm.ai-platform/deregister")
RECONCILE_INTERVAL_SEC = int(os.environ.get("RECONCILE_INTERVAL_SEC", "600"))
HTTP_TIMEOUT_SEC = int(os.environ.get("HTTP_TIMEOUT_SEC", "15"))
WATCH_TIMEOUT_SEC = int(os.environ.get("WATCH_TIMEOUT_SEC", "300"))

CR_GROUP = "kro.run"
CR_VERSION = "v1alpha1"

# Serving tiers this controller owns, and how to build each one's LiteLLM
# api_base from the CR name + namespace. All are kro.run/v1alpha1, cluster-wide.
#   vLLM (simple):     the model-server Service, port 8000
#   llm-d / disagg:    the llm-d Endpoint-Picker (EPP) Service, port 80
KINDS = {
    "vllmendpoints": "http://{name}-vllm.{ns}.svc.cluster.local:8000/v1",
    "llmdendpoints": "http://{name}-epp.{ns}.svc.cluster.local:80/v1",
    "llmddisaggendpoints": "http://{name}-epp.{ns}.svc.cluster.local:80/v1",
}

stop_event = threading.Event()


def _stop(*_: object) -> None:
    log.info("shutdown requested")
    stop_event.set()


# ---------------------------------------------------------------------------
# LiteLLM client (stdlib urllib — no extra deps beyond the k8s client)
# ---------------------------------------------------------------------------

def _litellm_request(method: str, path: str, body: dict | None = None) -> dict | None:
    """Call the LiteLLM admin API. Returns parsed JSON, or None on transport error.

    Raises nothing — callers treat None as "could not complete, retry later".
    """
    url = f"{LITELLM_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url=url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {LITELLM_MASTER_KEY}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:  # noqa: S310 (internal cluster URL)
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        log.warning("LiteLLM %s %s -> HTTP %s: %s", method, path, e.code, e.reason)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("LiteLLM %s %s failed: %s", method, path, e)
    return None


def list_db_models() -> dict[str, str] | None:
    """Return {model_name: model_id} for DB-registered models only.

    Static config-file models (db_model == False) are excluded so they can
    never be selected for deletion. Returns None if LiteLLM is unreachable.
    """
    info = _litellm_request("GET", "/model/info")
    if info is None:
        return None
    result: dict[str, str] = {}
    for entry in info.get("data", []) or []:
        model_info = entry.get("model_info") or {}
        if not model_info.get("db_model"):
            continue
        name = entry.get("model_name")
        model_id = model_info.get("id")
        if name and model_id:
            result[name] = model_id
    return result


def list_served_names() -> set[str] | None:
    """Names currently in the live router (/v1/models). None if unreachable."""
    served = _litellm_request("GET", "/v1/models")
    if served is None:
        return None
    return {m["id"] for m in served.get("data", []) or [] if m.get("id")}


def register_model(name: str, model_id: str, api_base: str) -> bool:
    """Ensure `name` is registered and live in LiteLLM's router.

    Idempotent + self-healing: if the name is already in the live /v1/models it's
    left as-is; otherwise any stale DB entry for the name is removed (covers a
    name lingering in the DB but absent from the router after a LiteLLM restart)
    and the model is (re-)added so it lands in the running router.
    """
    served = list_served_names()
    if served is None:
        return False
    if name in served:
        return True
    db_models = list_db_models()
    if db_models is None:
        return False
    if name in db_models:
        _litellm_request("POST", "/model/delete", {"id": db_models[name]})
        log.info("register %s: removed stale DB entry %s before re-adding", name, db_models[name])
    resp = _litellm_request("POST", "/model/new", {
        "model_name": name,
        "litellm_params": {"model": f"openai/{model_id}", "api_base": api_base, "api_key": "no-key"},
    })
    if resp is None:
        return False
    log.info("registered model %s -> %s", name, api_base)
    return True


def deregister_model(name: str) -> bool:
    """Delete a DB-registered model from LiteLLM by display name. Idempotent."""
    db_models = list_db_models()
    if db_models is None:
        log.warning("deregister %s: LiteLLM unreachable — will retry on reconcile", name)
        return False
    model_id = db_models.get(name)
    if model_id is None:
        log.info("deregister %s: not a DB-registered model (already gone or static) — skipping", name)
        return True
    resp = _litellm_request("POST", "/model/delete", {"id": model_id})
    if resp is None:
        return False
    log.info("deregistered model %s (id=%s) from LiteLLM", name, model_id)
    return True


# ---------------------------------------------------------------------------
# CR helpers
# ---------------------------------------------------------------------------

def _finalizers(obj: dict) -> list[str]:
    return list((obj.get("metadata") or {}).get("finalizers") or [])


def _has_deletion_timestamp(obj: dict) -> bool:
    return bool((obj.get("metadata") or {}).get("deletionTimestamp"))


def _api_base(plural: str, name: str, ns: str) -> str:
    return KINDS[plural].format(name=name, ns=ns)


def _model_id(obj: dict) -> str:
    # Upstream served-model-name — the serving tiers pass --served-model-name spec.model.
    return (obj.get("spec") or {}).get("model", "")


def _patch_finalizers(custom: client.CustomObjectsApi, plural: str, ns: str, name: str,
                      finalizers: list[str]) -> bool:
    patch = {"metadata": {"finalizers": finalizers}}
    try:
        custom.patch_namespaced_custom_object(
            group=CR_GROUP, version=CR_VERSION, namespace=ns,
            plural=plural, name=name, body=patch,
        )
        return True
    except ApiException as e:
        if e.status == 404:  # deleted out from under us — nothing to patch
            return True
        log.warning("patch finalizers on %s/%s (%s) failed: %s", ns, name, plural, e)
        return False


def process(custom: client.CustomObjectsApi, plural: str, obj: dict) -> None:
    """Route one serving-tier object to the right handler."""
    meta = obj.get("metadata") or {}
    name = meta.get("name")
    ns = meta.get("namespace", "")
    if not name:
        return

    if _has_deletion_timestamp(obj):
        current = _finalizers(obj)
        if FINALIZER not in current:
            return
        # Deregister first; only drop the finalizer once LiteLLM confirms, else
        # retry on the next event / reconcile.
        if not deregister_model(name):
            log.warning("keeping finalizer on %s/%s until deregistration succeeds", ns, name)
            return
        remaining = [f for f in current if f != FINALIZER]
        if _patch_finalizers(custom, plural, ns, name, remaining):
            log.info("removed finalizer from %s/%s — deletion can proceed", ns, name)
        return

    # Live object: ensure finalizer, then register.
    current = _finalizers(obj)
    if FINALIZER not in current:
        if _patch_finalizers(custom, plural, ns, name, current + [FINALIZER]):
            log.info("added finalizer to %s/%s", ns, name)
    model_id = _model_id(obj)
    if model_id:
        register_model(name, model_id, _api_base(plural, name, ns))


# ---------------------------------------------------------------------------
# Watch loop (one thread per kind, cluster-wide)
# ---------------------------------------------------------------------------

def watch_kind(plural: str) -> None:
    while not stop_event.is_set():
        try:
            custom = client.CustomObjectsApi()
            w = watch.Watch()
            stream = w.stream(
                custom.list_cluster_custom_object,
                group=CR_GROUP, version=CR_VERSION, plural=plural,
                timeout_seconds=WATCH_TIMEOUT_SEC,
            )
            for event in stream:
                if stop_event.is_set():
                    w.stop()
                    break
                obj = event.get("object")
                if not isinstance(obj, dict) or "metadata" not in obj:
                    continue  # 410 Gone / status object — let the loop re-list
                process(custom, plural, obj)
        except ApiException as e:
            # 404 = CRD not installed (e.g. the llm-d kinds before the
            # inference-gateway app has synced its GIE CRDs). Back off quietly; don't spin.
            if e.status == 404:
                time.sleep(60)
            else:
                log.warning("watch %s error (HTTP %s): %s — reconnecting", plural, e.status, e.reason)
                time.sleep(5)
        except Exception as e:  # noqa: BLE001 — never let the watch thread die
            log.warning("watch %s error: %s — reconnecting", plural, e)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Reconcile loop (backstop for missed events / controller downtime)
# ---------------------------------------------------------------------------

def reconcile_once(custom: client.CustomObjectsApi) -> None:
    """Repair finalizer/registration drift and sweep orphaned DB-registered models."""
    live_names: set[str] = set()
    any_kind_listed = False
    for plural in KINDS:
        try:
            items = custom.list_cluster_custom_object(
                group=CR_GROUP, version=CR_VERSION, plural=plural,
            ).get("items", [])
        except ApiException as e:
            if e.status != 404:
                log.warning("reconcile: list %s failed: %s", plural, e)
            continue
        any_kind_listed = True
        for obj in items:
            live_names.add(obj["metadata"]["name"])
            process(custom, plural, obj)

    # Sweep orphaned DB models only if we successfully listed at least one kind
    # (otherwise a transient API error could wrongly deregister everything).
    if not any_kind_listed:
        return
    db_models = list_db_models()
    if db_models is None:
        return
    for name in db_models:
        if name not in live_names:
            log.info("reconcile: orphaned model %s has no serving CR — deregistering", name)
            deregister_model(name)


def reconcile_loop() -> None:
    while not stop_event.is_set():
        try:
            reconcile_once(client.CustomObjectsApi())
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile error: %s", e)
        for _ in range(RECONCILE_INTERVAL_SEC):
            if stop_event.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------

def _health_server() -> None:
    """Tiny :8080 server — 200 when the K8s API is reachable, else 503."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                client.CustomObjectsApi().list_cluster_custom_object(
                    group=CR_GROUP, version=CR_VERSION, plural="vllmendpoints", limit=1,
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:  # noqa: BLE001
                self.send_response(503)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, *_: object) -> None:
            pass

    http.server.HTTPServer(("", 8080), Handler).serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not LITELLM_MASTER_KEY:
        log.error("LITELLM_MASTER_KEY is empty — cannot authenticate to LiteLLM")
        return 1

    config.load_incluster_config()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    threading.Thread(target=_health_server, daemon=True).start()
    for plural in KINDS:
        threading.Thread(target=watch_kind, args=(plural,), daemon=True, name=f"watch-{plural}").start()
    threading.Thread(target=reconcile_loop, daemon=True, name="reconcile").start()

    log.info(
        "litellm-sync started: kinds=%s finalizer=%s litellm=%s reconcile=%ds",
        ",".join(KINDS), FINALIZER, LITELLM_BASE_URL, RECONCILE_INTERVAL_SEC,
    )

    while not stop_event.is_set():
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
