# litellm-sync

The **single owner of LiteLLM's model registry**. Watches the three serving-tier
custom resources cluster-wide and registers each model with LiteLLM on create,
deregisters it on delete — so LiteLLM's `/v1/models` always matches what's deployed.

## Why this exists

Models are declared as KRO custom resources — `VLLMEndpoint` (simple vLLM),
`LLMDEndpoint` (llm-d scale tier), and `LLMDDisaggEndpoint` (llm-d prefill/decode
disaggregation). Something has to tell LiteLLM about them (`POST /model/new`) and
remove them when they're deleted.

Doing that with a per-CR registration Job (the previous design) has two problems:

1. **The master key would leak into workload namespaces.** A registration Job runs
   in the CR's namespace and needs the LiteLLM master key. That's fine in the shared
   `inference` namespace, but the platform now lets teams deploy models into their
   own `team-*` namespaces — putting the master key there violates
   *private-by-default* (the master key never leaves `inference`/`ai-platform`).
2. **KRO has no delete-hook**, so nothing deregistered a deleted model — every
   removal left a dead `/v1/models` entry.

Centralizing both directions in one controller solves both: the master key stays in
this pod only, and a Kubernetes finalizer gives us the on-delete hook.

## How it works

A single-replica Deployment (no custom image — `python:3.12-slim` + an
initContainer that pip-installs the kubernetes client) running
[scripts/litellm_sync.py](scripts/litellm_sync.py):

1. **Watches** `vllmendpoints`, `llmdendpoints`, and `llmddisaggendpoints`
   (`kro.run/v1alpha1`) across **all namespaces** (one watch thread per kind).
2. **Live CR** → PATCH adds the `litellm.ai-platform/deregister` finalizer, then
   **registers** the model: LiteLLM alias = the CR name, upstream served-model-name
   = `spec.model`, `api_base` per tier:
   - vLLM: `http://<name>-vllm.<ns>.svc.cluster.local:8000/v1`
   - llm-d / disagg: `http://<name>-epp.<ns>.svc.cluster.local:80/v1`
3. **CR with `deletionTimestamp`** → deregister the model, then PATCH removes the
   finalizer so deletion completes. The finalizer is only dropped *after*
   deregistration succeeds; if LiteLLM is unreachable it stays put and retries.
4. **Reconcile every 10 min** (backstop): re-add finalizers, re-register any model
   missing from the live router (repairs drift after a LiteLLM restart, which
   reloads only the static config), and sweep orphaned DB-registered models whose
   CR no longer exists.

Registration is idempotent and self-healing: a model already live in `/v1/models`
is left alone; otherwise a stale DB entry is removed and the model re-added so it
lands back in the running router.

### Static models are protected

Deregistration only ever touches **DB-registered** models (`model_info.db_model ==
true`). Static config-file models declared in `litellm.yaml` — notably the Bedrock
`claude-opus-4-8` baseline — report `db_model: false` and are skipped. They can
never be deleted by this controller.

## Operational notes

**Stuck `Terminating` (rare).** Because this uses a true finalizer, if the
controller is down at the exact moment a CR is deleted, the object stays in
`Terminating` until the controller comes back — the reconcile loop and the
Deployment's auto-restart make this self-healing in practice. To force-unstick:

```bash
kubectl patch <vllmendpoint|llmdendpoint|llmddisaggendpoint> <name> -n <ns> \
  --type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]'
```

(The reconcile sweep will then deregister the now-orphaned model on its next pass.)

**Relationship to `litellm-cleanup`.** This controller owns **model** consistency.
The hourly `litellm-model-cleanup` CronJob (`platform/config/litellm-cleanup.yaml`)
owns **team** consistency only (orphaned LiteLLM teams from deleted `AITeam`s).

## Config (env vars, see `deployment.yaml`)

| Var | Default | Purpose |
| --- | --- | --- |
| `LITELLM_BASE_URL` | `http://litellm.ai-platform.svc.cluster.local:4000` | LiteLLM admin API |
| `LITELLM_MASTER_KEY` | _(from `litellm-api-key` secret)_ | Bearer auth for the admin API |
| `FINALIZER` | `litellm.ai-platform/deregister` | Finalizer string the controller manages |
| `RECONCILE_INTERVAL_SEC` | `600` | Backstop reconcile cadence |
