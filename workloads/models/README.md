# Models — self-service deployment

Deploy a model with GitOps: add a `VLLMEndpoint` YAML and `git push`. ArgoCD
(the `workloads-models` ApplicationSet) discovers it, and the platform provisions
a GPU node, loads the model on vLLM, and registers it with the LiteLLM gateway
(centrally, via `litellm-sync`). Removal is `git rm`.

## Directory = namespace

`workloads/models/<namespace>/` — **each subdirectory is the target namespace**
the models inside it deploy into:

- `workloads/models/inference/` — platform-shared models (the `inference` namespace).
- `workloads/models/team-<name>/` — a team's models, landing in that team's
  quota'd, RBAC'd namespace with its scoped API key. **Onboard the team first**:
  add an `AITeam` in `workloads/teams/` (it creates the `team-<name>` namespace
  with a GPU quota + scoped LiteLLM key), then the team drops model YAMLs here.
  Models never create namespaces — so a stray commit can't spin up an unquota'd
  one; if the namespace doesn't exist yet, the app simply waits.

Do **not** set `metadata.namespace` in the model YAML — it inherits the
directory's namespace. Copy `TEMPLATE.yaml.example` into the right subdirectory,
e.g. `workloads/models/team-search/qwen3-3b.yaml`.

For the scale / disaggregation tiers, use `workloads/scale-models/`
(`LLMDEndpoint` / `LLMDDisaggEndpoint`).

## Separate workloads repo (multi-team)

By default, models live in this repo. For real multi-team self-service, point
`var.gitops_workloads_repo_url` at a separate, tenant-owned repo: teams get write
access to the **workloads repo only** — never the platform repo. The
directory-per-namespace convention above is unchanged.

## Bedrock — available out of the box

Bedrock's `claude-opus-4-8` is a static LiteLLM entry (no GPUs, no CR), so a
fresh install serves against it immediately. To serve any other model, point a
`VLLMEndpoint` at a HuggingFace model ID — including a model you've fine-tuned and
pushed to HF (private repos need a token). The platform serves models; it does not
train them. (Serving weights directly from your own S3 bucket is on the roadmap.)

## Tool / function calling

`platformctl new-model` auto-detects tool/function-calling support from the
model's architecture and writes `toolCallParser: <parser>` into the generated
`VLLMEndpoint` (e.g. `gemma4`, `hermes` for Qwen, `mistral`, `llama3_json`). The
model then answers `tool_choice: auto` requests — from Open WebUI or the API —
out of the box instead of erroring. It's written explicitly so you can see and
change it: delete the line for chat-only, or adjust it if you pin a different
`vllmImage` (parser names are vLLM-version-specific). Models from unrecognized
families deploy chat-only (no `toolCallParser`) — add one by hand if the model
supports tools. For any other vLLM flag, use `extraArgs: ["--flag", "value"]`.
