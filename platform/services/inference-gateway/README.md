# inference-gateway — llm-d scale-tier substrate

The minimal cluster substrate the **llm-d scale tier** (`LLMDEndpoint`) needs:

- **`kustomization.yaml`** — installs the Gateway API Inference Extension CRDs
  (notably `InferencePool`), pulled from upstream at a **pinned tag** (`v1.5.0`).
  The llm-d router chart creates an `InferencePool` per model. ArgoCD
  auto-detects this kustomization and runs `kustomize build` at sync time to
  fetch the CRDs; to upgrade, bump the `?ref=` tag.
- **`repo-secret.yaml`** — registers the `ghcr.io/llm-d/charts` OCI Helm registry
  with ArgoCD, so the `Application` rendered by the `LLMDEndpoint` RGD can pull
  the `llm-d-router-standalone` chart.

## Why this is all that's here

The productized scale tier runs the llm-d router in **standalone mode** (the
router carries its own Envoy proxy + EPP per `InferencePool`). Per the llm-d
guide, standalone mode requires **only the GIE CRDs** — no shared Envoy Gateway.
Ingress is **ALB → LiteLLM**, so
there is no Envoy front door either. The earlier Envoy Gateway / Envoy AI Gateway
/ Gateway-CR stack was therefore unused and has been removed.

## How it's delivered

A default ArgoCD platform component, delivered by the `platform` ApplicationSet
(`argocd/bootstrap/platform.yaml`, the `inference-gateway` element, infra tier) —
exactly like `gpu-operator`. It ships on every cluster: the footprint
is minimal (CRDs + a repo Secret, no running pods), and shipping it by default
means teams can use `LLMDEndpoint` without a separate cluster toggle. Terraform is
not involved.
