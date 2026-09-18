#!/usr/bin/env python3
"""litellm-sync — the single owner of LiteLLM's model registry.

Watches the serving-tier custom resources AND BedrockModels cluster-wide and
keeps LiteLLM's model list in sync, so the master key never has to enter a
workload namespace and models can be deployed into any namespace (e.g. per-team
`team-*` namespaces):

  Serving tiers (kro.run/v1alpha1), each in ANY namespace:
    - vllmendpoints        -> api_base http://<name>-vllm.<ns>.svc.cluster.local:8000/v1
    - llmdendpoints        -> api_base http://<name>-epp.<ns>.svc.cluster.local:80/v1
    - llmddisaggendpoints  -> api_base http://<name>-epp.<ns>.svc.cluster.local:80/v1

  Bedrock models (bedrock.ai-platform/v1alpha1), in ANY namespace:
    - bedrockmodels        -> native LiteLLM bedrock/ upstream (no api_base;
                              auth = the litellm pod's IRSA role)

Lifecycle (finalizer-driven, self-healing):

  1. WATCH each kind cluster-wide.
  2. Live object WITHOUT our finalizer      -> PATCH to add it, then REGISTER the
     model with LiteLLM (POST /model/new). LiteLLM alias = the CR name; upstream
     served-model-name = spec.model.
  3. Object WITH deletionTimestamp          -> DEREGISTER the model, then PATCH to
     remove our finalizer so deletion can complete.
  4. RECONCILE every RECONCILE_INTERVAL_SEC -> re-add finalizers, ensure every
     live CR's model is registered (and collapse any duplicate rows), and
     deregister orphaned DB-registered models whose CR no longer exists.

Registration is idempotent, duplicate-safe, and serialized under a single lock
(REGISTRY_LOCK) shared by the watch threads and the reconcile loop. It is
*monotonic*: it only POSTs /model/new when the name has NO DB row, and otherwise
only deletes extra rows (keep one, delete the rest). Never adding while a row
exists means concurrent callers reading LiteLLM's eventually-consistent
/model/info can only ever shrink the row count — so a model always converges to
exactly one entry and can never accumulate duplicates. Deregistration deletes
ALL rows for a name and only ever touches DB-registered models
(model_info.db_model == True); a model is swept only when NO live CR still claims
its name, so every CR-backed model — including Bedrock models enrolled via
`platformctl new-model --source bedrock` — is protected. Any static config-file
model in litellm.yaml (db_model == False) is excluded outright and never deleted.

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

CR_VERSION = "v1alpha1"

# Serving tiers (KRO, group kro.run): registered as an in-cluster,
# OpenAI-compatible upstream. Value = how to build each one's LiteLLM api_base
# from the CR name + namespace. All cluster-wide.
#   vLLM (simple):     the model-server Service, port 8000
#   llm-d / disagg:    the llm-d Endpoint-Picker (EPP) Service, port 80
SERVING_GROUP = "kro.run"
SERVING_KINDS = {
    "vllmendpoints": "http://{name}-vllm.{ns}.svc.cluster.local:8000/v1",
    "llmdendpoints": "http://{name}-epp.{ns}.svc.cluster.local:80/v1",
    "llmddisaggendpoints": "http://{name}-epp.{ns}.svc.cluster.local:80/v1",
}

# Bedrock models (plain CRD, group bedrock.ai-platform): registered as a native
# LiteLLM Bedrock upstream (model: bedrock/<id>, NO api_base — auth at call time
# is the litellm pod's IRSA role). Authored by
# `platformctl new-model --source bedrock`. Same finalizer/register/reconcile
# lifecycle as the serving tiers; only the emitted litellm_params differ.
BEDROCK_GROUP = "bedrock.ai-platform"
BEDROCK_KINDS = {"bedrockmodels"}

# Every watched CR kind -> its API group. One watch thread per kind.
KIND_GROUP = {
    **{plural: SERVING_GROUP for plural in SERVING_KINDS},
    **{plural: BEDROCK_GROUP for plural in BEDROCK_KINDS},
}
KINDS = list(KIND_GROUP)   # stable iteration order for watch threads + reconcile

stop_event = threading.Event()

# Serializes ALL LiteLLM registry mutations (register/deregister). The watch
# threads (one per kind) and the reconcile thread can otherwise call into the
# registry concurrently for the same model; without this lock two callers can
# both observe "not registered" and each POST /model/new, creating duplicate DB
# rows for one CR (the symptom: a model showing up 2-3× in the LiteLLM UI).
REGISTRY_LOCK = threading.Lock()


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


def list_db_model_ids() -> dict[str, list[tuple[str, dict]]] | None:
    """Return {model_name: [(model_id, litellm_params), ...]} for DB-registered
    models only.

    Preserves EVERY DB row for a name (so duplicate registrations are visible and
    can be collapsed) and carries each row's live litellm_params (so register_model
    can detect when a CR's params have drifted from what's registered). Static
    config-file models (db_model == False) are excluded so they can never be
    selected for deletion. Returns None if LiteLLM is unreachable.
    """
    info = _litellm_request("GET", "/model/info")
    if info is None:
        return None
    result: dict[str, list[tuple[str, dict]]] = {}
    for entry in info.get("data", []) or []:
        model_info = entry.get("model_info") or {}
        if not model_info.get("db_model"):
            continue
        name = entry.get("model_name")
        model_id = model_info.get("id")
        params = entry.get("litellm_params") or {}
        if name and model_id:
            result.setdefault(name, []).append((model_id, params))
    return result


# litellm_params keys this controller sets and can meaningfully diff. api_key is
# deliberately excluded: LiteLLM redacts it in /model/info, so comparing it would
# always report drift and churn.
_DRIFT_KEYS = ("model", "api_base", "aws_bedrock_runtime_endpoint",
               "input_cost_per_token", "output_cost_per_token")


def _params_drifted(current: dict, desired: dict) -> bool:
    """True when a param the CR now specifies differs from what LiteLLM has.

    Conservative by design so it can never churn: a key that LiteLLM does NOT
    echo back in `current` is treated as "can't compare -> not drift" (so if a
    given LiteLLM build omits e.g. cost fields from /model/info, that edit simply
    isn't hot-applied — the pre-existing documented behavior — rather than
    re-registering every reconcile). Only a key present in BOTH and differing
    counts. Costs compared with a small relative tolerance; other keys exact.
    """
    current = current or {}
    for k in _DRIFT_KEYS:
        if k not in desired or k not in current:
            continue
        dv, cv = desired[k], current[k]
        if k in ("input_cost_per_token", "output_cost_per_token"):
            try:
                if abs(float(cv) - float(dv)) > abs(float(dv)) * 1e-6 + 1e-15:
                    return True
            except (TypeError, ValueError):
                if str(cv) != str(dv):
                    return True
        elif cv != dv:
            return True
    return False


def register_model(name: str, litellm_params: dict) -> bool:
    """Ensure `name` is registered exactly once in LiteLLM. Duplicate-safe.

    `litellm_params` is the provider-specific block (an in-cluster openai/ + api_base
    for the serving tiers, or a native bedrock/ upstream for BedrockModels).

    Held under REGISTRY_LOCK so a concurrent watch + reconcile can't both add the
    same model. It is *near-monotonic*: with unchanged params it only ADDS when
    there is no DB row for the name and otherwise only REMOVES extra rows (keep
    the first, delete the rest); the sole exception is a real params change, which
    re-registers the one kept row (see "Param drift" below).

    That monotonicity is what makes it robust against LiteLLM's eventually-
    consistent /model/info: an earlier delete-all-then-add design could act on a
    stale read (see fewer rows than really exist, delete those, add one) and so
    *grow* duplicates under startup churn. Because this version never adds while a
    row is present EXCEPT to apply a real params change, repeated passes with
    unchanged params can only shrink the row count — so it always converges to
    exactly one.

    Param drift: when a row already exists but the CR's litellm_params have
    changed (e.g. an edited Bedrock price/endpoint), we re-register (delete the
    kept row + add with the new params) under the lock so LiteLLM reflects the
    edit. Drift is detected conservatively (see _params_drifted) — it can only
    trigger on a genuine change, never on steady state — so this does not
    reintroduce churn.
    """
    with REGISTRY_LOCK:
        db_models = list_db_model_ids()
        if db_models is None:
            return False
        rows = db_models.get(name, [])
        if rows:
            keep_id, keep_params = rows[0]
            for extra_id, _ in rows[1:]:
                _litellm_request("POST", "/model/delete", {"id": extra_id})
            if len(rows) > 1:
                log.info("register %s: removed %d duplicate DB row(s)", name, len(rows) - 1)
            if _params_drifted(keep_params, litellm_params):
                # CR spec changed — re-register so the edit takes effect. Rare (an
                # apply), so delete+add here can't churn on steady state.
                _litellm_request("POST", "/model/delete", {"id": keep_id})
                resp = _litellm_request("POST", "/model/new", {
                    "model_name": name,
                    "litellm_params": litellm_params,
                })
                if resp is None:
                    return False
                log.info("re-registered model %s — CR litellm_params changed", name)
            return True
        resp = _litellm_request("POST", "/model/new", {
            "model_name": name,
            "litellm_params": litellm_params,
        })
        if resp is None:
            return False
        log.info("registered model %s (%s)", name, litellm_params.get("model"))
        return True


def deregister_model(name: str) -> bool:
    """Delete ALL DB-registered rows for a model by display name. Idempotent.

    Removes every row sharing the name (not just one) so a model that was
    accidentally registered multiple times is fully removed. Held under
    REGISTRY_LOCK to stay consistent with register_model.
    """
    with REGISTRY_LOCK:
        db_models = list_db_model_ids()
        if db_models is None:
            log.warning("deregister %s: LiteLLM unreachable — will retry on reconcile", name)
            return False
        rows = db_models.get(name, [])
        if not rows:
            log.info("deregister %s: not a DB-registered model (already gone or static) — skipping", name)
            return True
        ok = True
        for model_id, _ in rows:
            if _litellm_request("POST", "/model/delete", {"id": model_id}) is None:
                ok = False
        if ok:
            log.info("deregistered model %s (%d row(s)) from LiteLLM", name, len(rows))
        return ok


# ---------------------------------------------------------------------------
# CR helpers
# ---------------------------------------------------------------------------

def _finalizers(obj: dict) -> list[str]:
    return list((obj.get("metadata") or {}).get("finalizers") or [])


def _has_deletion_timestamp(obj: dict) -> bool:
    return bool((obj.get("metadata") or {}).get("deletionTimestamp"))


def _api_base(plural: str, name: str, ns: str) -> str:
    return SERVING_KINDS[plural].format(name=name, ns=ns)


def _spec(obj: dict) -> dict:
    return obj.get("spec") or {}


def _model_alias(plural: str, obj: dict) -> str:
    """The LiteLLM model_name (alias) for a CR. Serving tiers use the CR name; a
    BedrockModel may override it via spec.modelName (default: the CR name).
    This is the name registered in LiteLLM and tracked by the reconcile sweep."""
    name = (obj.get("metadata") or {}).get("name", "")
    if plural in BEDROCK_KINDS:
        return _spec(obj).get("modelName") or name
    return name


def _litellm_params_for(plural: str, obj: dict) -> dict | None:
    """Build the LiteLLM litellm_params for a CR, or None if it isn't ready to
    register (no spec.model)."""
    spec = _spec(obj)
    model = spec.get("model")
    if not model:
        return None
    if plural in BEDROCK_KINDS:
        # Native Bedrock upstream: no api_base/api_key (auth = the litellm pod's
        # IRSA role). spec.model already carries the "bedrock/<id>" provider
        # string with the region/partition-correct invocation id.
        params: dict = {"model": model}
        endpoint = spec.get("bedrockEndpoint")
        if endpoint:
            params["aws_bedrock_runtime_endpoint"] = endpoint
        name = (obj.get("metadata") or {}).get("name", "")
        for cr_key, ll_key in (("inputCostPerToken", "input_cost_per_token"),
                               ("outputCostPerToken", "output_cost_per_token")):
            val = spec.get(cr_key)
            if val in (None, ""):
                continue
            try:
                params[ll_key] = float(val)
            except (TypeError, ValueError):
                log.warning("bedrockmodel %s: ignoring non-numeric %s=%r", name, cr_key, val)
        return params
    # Serving tiers: in-cluster OpenAI-compatible upstream.
    meta = obj.get("metadata") or {}
    return {
        "model": f"openai/{model}",
        "api_base": _api_base(plural, meta.get("name", ""), meta.get("namespace", "")),
        "api_key": "no-key",
    }


def _patch_finalizers(custom: client.CustomObjectsApi, plural: str, ns: str, name: str,
                      finalizers: list[str]) -> bool:
    patch = {"metadata": {"finalizers": finalizers}}
    try:
        custom.patch_namespaced_custom_object(
            group=KIND_GROUP[plural], version=CR_VERSION, namespace=ns,
            plural=plural, name=name, body=patch,
        )
        return True
    except ApiException as e:
        if e.status == 404:  # deleted out from under us — nothing to patch
            return True
        log.warning("patch finalizers on %s/%s (%s) failed: %s", ns, name, plural, e)
        return False


def process(custom: client.CustomObjectsApi, plural: str, obj: dict) -> None:
    """Route one CR (serving tier or BedrockModel) to the right handler."""
    meta = obj.get("metadata") or {}
    name = meta.get("name")
    ns = meta.get("namespace", "")
    if not name:
        return
    alias = _model_alias(plural, obj)

    if _has_deletion_timestamp(obj):
        current = _finalizers(obj)
        if FINALIZER not in current:
            return
        # Deregister first; only drop the finalizer once LiteLLM confirms, else
        # retry on the next event / reconcile.
        if not deregister_model(alias):
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
    params = _litellm_params_for(plural, obj)
    if params:
        register_model(alias, params)


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
                group=KIND_GROUP[plural], version=CR_VERSION, plural=plural,
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
                group=KIND_GROUP[plural], version=CR_VERSION, plural=plural,
            ).get("items", [])
        except ApiException as e:
            if e.status != 404:
                log.warning("reconcile: list %s failed: %s", plural, e)
            continue
        any_kind_listed = True
        for obj in items:
            live_names.add(_model_alias(plural, obj))
            process(custom, plural, obj)

    # Sweep orphaned DB models only if we successfully listed at least one kind
    # (otherwise a transient API error could wrongly deregister everything).
    if not any_kind_listed:
        return
    db_models = list_db_model_ids()
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
    """Tiny :8080 server — 200 when the K8s API is reachable and listable (or the
    watched CRD is simply absent: 404); 503 on RBAC denial (403/401) or transport
    failure, so a controller that can reconcile nothing never reports healthy."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                client.CustomObjectsApi().list_cluster_custom_object(
                    group=SERVING_GROUP, version=CR_VERSION, plural="vllmendpoints", limit=1,
                )
            except ApiException as e:
                # ONLY 404 is benign: the CRD isn't installed (a Bedrock-only /
                # kro=false install has no vllmendpoints CRD), and the watch/
                # reconcile loops back off until it appears. Any OTHER status is a
                # real fault the probe MUST surface — in particular 403/401 means
                # the ServiceAccount can't list the CRs it exists to reconcile
                # (broken/rolled-back RBAC), so the controller registers nothing;
                # reporting healthy there would hide a dead controller behind a
                # green probe.
                if e.status != 404:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(f"kubernetes API error {e.status}: {e.reason}".encode())
                    return
            except Exception as e:  # noqa: BLE001
                self.send_response(503)
                self.end_headers()
                self.wfile.write(str(e).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

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
