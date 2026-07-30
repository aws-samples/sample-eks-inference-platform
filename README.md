# Inference Platform on Amazon EKS

**A self-service, multi-tenant inference platform for your own AWS account.** One
OpenAI-compatible API fronts every model — Amazon Bedrock and any Hugging Face
model served on GPUs — with per-team keys, budgets, and rate limits. Teams ship models the way
they ship code: commit a short YAML, `git push`, and the platform handles GPU
provisioning, serving, routing, and observability. A frontier model
(**Bedrock Claude Opus 4.8**) works on day one with **zero GPUs**.

**Two things make it work:**

- **One gateway, every model.** LiteLLM puts Bedrock and any vLLM-served Hugging
  Face model (including your fine-tuned ones) behind a single
  `/v1/chat/completions` endpoint — with team isolation, budgets, and Langfuse
  tracing built in.
- **Extendable templates.** Four [KRO](https://kro.run) resources
  (`VLLMEndpoint`, `LLMDEndpoint`, `LLMDDisaggEndpoint`, `AITeam`)
  capture the hard parts — tensor-parallelism, GPU sizing, elastic autoscaling,
  scale-tier routing, prefill/decode disaggregation — as a few lines of YAML.
  They're the platform's API: fork and extend them, don't reinvent them.

**Stack:** EKS Managed Capabilities (ArgoCD · KRO · ACK) · Karpenter · vLLM ·
LiteLLM · Langfuse — with an optional **llm-d + Gateway API Inference
Extension** scale tier.

![Cluster dashboard — live topology of nodes, GPU slots, and deployed models](docs/img/cluster-dashboard.png)

---

## Architecture

```
git push → ArgoCD syncs → KRO expands your YAML into K8s + AWS resources
         → Karpenter provisions a GPU node → vLLM loads the model
         → LiteLLM registers it → available via API, Open WebUI, and Langfuse
```

The custom resources **are** the self-service interface:

| Resource | What it does |
|---|---|
| **`VLLMEndpoint`** | Serve a model on vLLM — the simple default: one model, one pod, one instance (any Hugging Face model ID) |
| **`LLMDEndpoint`** | Serve a model on the llm-d scale tier — KV-cache/load/prefix-aware routing across replicas (the `inference-gateway` substrate ships on every cluster; no toggle) |
| **`LLMDDisaggEndpoint`** | Serve on the llm-d scale + performance tier — independently autoscaled prefill/decode pools (same llm-d substrate; no toggle) |
| **`AITeam`** | Onboard a team: namespace, RBAC, budget, rate limits, scoped API key |

```yaml
# That's the whole interface — e.g. serve a model:
apiVersion: kro.run/v1alpha1
kind: VLLMEndpoint
metadata: { name: qwen3-3b, namespace: inference }
spec:
  model: "Qwen/Qwen2.5-3B-Instruct"   # ungated — no token needed
  gpuCount: 1                          # 1/2/4/8 → vLLM tensor parallelism
  shared: false                        # true → time-slice one GPU across up to 4 small models
```

Bedrock models need no resource — they're a few lines of LiteLLM config, live the
moment the cluster is up. KRO definitions live in `platform/config/kro/`; extend
them there and every model/team inherits the change.

Every model answers through the same LiteLLM `/v1` API, so governance, budgets, and
tracing apply uniformly — including the optional **llm-d** scale tier
(`LLMDEndpoint` / `LLMDDisaggEndpoint`), which LiteLLM forwards to internally.

---

## Prerequisites

**Tools** (on the machine you run `./platformctl` from):
- **AWS CLI v2** with credentials configured (`aws sts get-caller-identity` must work), plus the
  [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (for `./platformctl tunnel`)
- **Terraform**, **kubectl**, **make**, **jq**, **git**, and **python3** with **boto3**

**AWS account setup**:
- An **IAM Identity Center** instance — its ARN goes in the tfvars (ArgoCD SSO)
- **Amazon Bedrock model access** enabled in your region (for the day-one Claude Opus — enable it in the Bedrock console)
- Enough **service quota** for the GPU instance types you plan to self-host on (not needed for the Bedrock-only path)
- A **fork of this repo** that ArgoCD can read — its URL goes in `gitops_repo_url`

**Configure** — copy `terraform/00.global/vars/example.tfvars` to `<env>.tfvars` and fill the `REPLACE` markers: your Identity Center ARN, `gitops_repo_url` (your fork), `region`, and a unique `resources_prefix`.

> Keep `private_eks_cluster = false` (the default). A private-only cluster's API is
> reachable only from inside the VPC, so `./platformctl up` from a laptop can't
> provision it — the Kubernetes resources time out on the private endpoint. Only
> set it `true` if you run Terraform from an in-VPC host (bastion / CloudShell-in-VPC / VPN).

## Quick start

> ⚠️ **Before you deploy — this creates real, billable infrastructure in your AWS
> account.** It provisions an EKS cluster and (on demand) GPU nodes. The platform
> UIs sit behind an **internal ALB** by default (no public IP) — reach them via
> `./platformctl tunnel` or the opt-in CloudFront edge. If you switch the ALB to
> **internet-facing**, restrict it to your own IP ranges via the **IP allowlist**
> first — never leave it open to the public internet (`0.0.0.0/0`). GPU nodes and
> the cluster incur significant cost; use [Cleanup](#cleanup) to remove
> everything when finished. See [SECURITY.md](SECURITY.md).


```bash
# 1. Configure: copy the template, then set your Identity Center ARN, gitops repo URL, and region.
cd terraform/00.global/vars && cp example.tfvars dev.tfvars   # edit dev.tfvars — fill every REPLACE marker

# 2. Provision everything (VPC → EKS + capabilities → Karpenter → secrets).
#    platformctl reads `region` from dev.tfvars and pins AWS_REGION for you.
./platformctl up dev

# 3. Use it immediately — no GPUs yet (up already pointed kubectl at the new cluster).
./platformctl tunnel        # forward the UIs (WebUI / LiteLLM / Langfuse)
./platformctl status --check  # verify Bedrock + models answer AND Langfuse tracing works

# 4. Deploy a self-hosted model with one command. `new-model` right-sizes it and
#    ships it end to end:
#      - reads the model's config from Hugging Face and computes its VRAM +
#        tensor-parallelism needs;
#      - picks a cost-ranked GPU instance type allowed by your Karpenter NodePools;
#      - scaffolds the matching endpoint CRD — VLLMEndpoint, or an LLMDEndpoint /
#        LLMDDisaggEndpoint for the llm-d scale tier (--tier, else auto-selected);
#      - with --deploy: writes it to workloads/models/inference/<name>.yaml, then
#        git commits + pushes and nudges ArgoCD to sync.
#    ArgoCD applies it -> Karpenter provisions a GPU node -> vLLM loads the model
#    -> LiteLLM registers it on the /v1 API.
./platformctl new-model Qwen/Qwen2.5-3B-Instruct --deploy   # add -y to skip the confirm prompt
kubectl get vllmendpoints -n inference -w                   # watch it come up (GPU cold start ~ a few min)
```

Drop `--deploy` to just print the recommendation and the ready-to-commit YAML for
review (nothing is pushed). Size for your traffic with `--seq`, `--users`, or
`--workload`, force a serving tier with `--tier`, or point at a private/gated model
with `--hf-token` — see `./platformctl new-model --help` for all flags. Removing a
model is the mirror image: `./platformctl new-model <name> --undeploy` (or just
`git rm` its YAML).

> ⚠️ **The recommended instance type is a sizing guide, not a guarantee.** It's
> computed from the model's memory footprint and current on-demand pricing. What
> Karpenter actually launches is decided at provisioning time from *real* capacity:
> it picks a compatible type from the GPU NodePool's allowed set based on what's
> available in your region/AZs at that moment — so the node you get (and its
> hourly cost) may differ from the recommendation. The model still fits and serves
> correctly; only the specific instance may vary.

---

## Beyond the basics

**Serve a fine-tuned model.** Any HuggingFace model ID works — including a model
you've fine-tuned and pushed to HF (public, or private with a token). Point a
`VLLMEndpoint` (or `LLMDEndpoint`) at its HF ID and ship it with the same
`git push` loop. The platform **serves** models; you bring the training (fine-tuning
itself is out of scope).

**Scale-tier routing (llm-d).** For high-QPS or long, multi-turn/agentic
workloads, commit an `LLMDEndpoint` (see `workloads/scale-models/`) and the
optional llm-d tier schedules requests across vLLM replicas using live KV-cache,
prefix, and queue-depth signals, and supports prefill/decode disaggregation.

**Fast cold starts (opt-in).** New GPU deployments can shave the multi-minute cold
start via three layers, wired through Terraform's image optimization and switched on
by setting `docker_hub_username` (which enables the ECR pull-through cache): EBS
image snapshots (near-instant image pull) and SOCI lazy-loading, plus an S3
model-weight cache — pre-seed a model's HuggingFace weights there and the serving
initContainer loads them from local disk instead of pulling from HuggingFace. Actual
savings vary by model and instance.

**Platform Health Agent.** The cluster dashboard can watch for failures, investigate
them with an LLM, and propose a one-click fix — idle until you provide a Kiro key.
See **[its guide](platform/services/cluster-dashboard/PLATFORM-HEALTH-AGENT.md)**.

**Cost control.** Karpenter right-sizes and consolidates GPU nodes to match demand
and reclaims them when workloads are removed; `shared: true` time-slices one
physical GPU across up to 4 small models.

**Team self-service (GitOps).** Onboard a team with an `AITeam` YAML in
`workloads/teams/` — it creates a `team-<name>` namespace with a GPU quota, RBAC,
namespace isolation (a default-deny **ingress** NetworkPolicy — only same-team and
platform namespaces can reach in), and a scoped LiteLLM key (budget + rpm/tpm). The team then
deploys models by committing a `VLLMEndpoint` under **`workloads/models/team-<name>/`**
— the directory name is the target namespace, so models land in that team's quota
and key (no `kubectl`, no console; removal is `git rm`). By default workloads live
in this repo; for real multi-team self-service point `gitops_workloads_repo_url` at
a separate, tenant-owned repo so teams get write access to the workloads repo only,
never the platform repo. See [`workloads/models/README.md`](workloads/models/README.md).

**Single sign-on, per-user cost & budgets.** SSO ships enabled (`enable_sso`,
default on): Terraform stands up an **Amazon Cognito** user pool with a hosted login
page, role groups (`admins`/`developers`/`users`), and three seed users — log in
by email (`admin@example.com` / `developer@example.com` / `user@example.com`),
whose generated passwords are the `sso_seed_user_passwords` Terraform output —
retrieve them any time (the `up` output scrolls away) with:

```bash
TF_WORKSPACE=<env> terraform -chdir=terraform/30.eks/30.cluster output -json sso_seed_user_passwords
```

Open WebUI, the LiteLLM admin UI, and Langfuse all federate to Cognito, and Open
WebUI forwards the signed-in identity so **cost is attributed per user** in
LiteLLM's spend reports.
LiteLLM also enforces a **default per-user budget + rpm/tpm throttle** on the chat
path (from that forwarded identity, no keys needed) and caps any API key a user
mints for themselves — tune the defaults in
[`platform/services/litellm/litellm.yaml`](platform/services/litellm/litellm.yaml).
It works out of the box over `./platformctl tunnel` (Cognito allows `localhost`
callbacks); bring your own enterprise IdP by federating it into the pool. Cognito is
the only new hard dependency — Identity Center stays required only for ArgoCD SSO.
For **public HTTPS** access (and to protect the dashboard behind auth), opt in to
the CloudFront edge with `./platformctl edge cloudfront` — Terraform stands up a
CloudFront **VPC origin** to the private ALB, with a free `*.cloudfront.net`
certificate (no domain needed) and the Cognito callbacks wired automatically. For
your own domain, `./platformctl edge domain` (internet-facing ALB + your ACM cert).
See **[docs/cloudfront-edge.md](docs/cloudfront-edge.md)** for how it works and the
edge gotchas the Terraform handles.

---

## Cleanup

```bash
./platformctl down <env>          # → make destroy-all ENVIRONMENT=<env>
                                  #   prompts you to type the env name to confirm
```

`destroy-all` walks the six terraform stages in reverse
(`oss-obs → native-obs → addons → cluster → iam → networking`). On a healthy
cluster that has been idle, it finishes in ~25 minutes. On a cluster that's
been actively running models, expect **30–45 min** and a few hand-cleanup
steps below. The script does **not** touch the bootstrap state (S3
`tfstate-<account>` + DynamoDB `tfstate-lock`), so a subsequent
`./platformctl up <env>` still works.

### Things you'll hit (and the cause)

* **`Error acquiring the state lock`** — a previous `terraform` run was
  killed mid-flight. Find the lock ID in the error and run
  `terraform -chdir=terraform/<stage> force-unlock -force <id>`.
* **`kubernetes_namespace.ai_platform: Still destroying...`** for many
  minutes — a `NetworkPolicy` finalizer (`networking.k8s.aws/resources`)
  is waiting for the VPC CNI controller, which has already been destroyed.
  Strip the finalizers manually:
  `kubectl get networkpolicies -n ai-platform -o name | xargs -I {} kubectl patch -n ai-platform {} --type=merge -p '{"metadata":{"finalizers":[]}}'`
* **`ECR Repository ... not empty`** — empty it with `aws ecr batch-delete-image`, then re-run.
* **Subnet stuck destroying for 15+ min** — almost always an orphan
  Karpenter EC2 instance still pinning the subnet; terminate it, then re-run.

When in doubt, the final state should match this:

```bash
# Should return empty for the env you destroyed:
aws eks list-clusters --query 'clusters[?contains(@, `<env>`)]'
aws ec2 describe-vpcs --filters "Name=tag:Environment,Values=<env>" --query 'Vpcs[].VpcId'
aws s3 ls | grep "<env>"
aws iam list-roles --query "Roles[?contains(RoleName, '<cluster-name>')].RoleName"
```

---

## Repository layout

```
argocd/bootstrap/   ApplicationSets (platform services + self-service workloads)
platform/
  config/kro/       VLLMEndpoint · LLMDEndpoint · LLMDDisaggEndpoint · AITeam (the API)
  services/         litellm, litellm-sync, open-webui, langfuse, gpu-operator,
                    cluster-dashboard (+ Platform Health Agent), inference-gateway
workloads/          Self-service YAMLs: models/ · scale-models/ · teams/
platformctl         The unified CLI (use · up · status · tunnel · edge · new-model · down · list-envs)
ops/                platformctl implementation: ops/lib/ (helpers) · ops/image/ (cold-start build helpers)
terraform/          Infrastructure modules (VPC → IAM → EKS → observability)
docs/               cloudfront-edge
```

## Acknowledgments

Infrastructure based on [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://aws-solutions-library-samples.github.io/compute/automated-provisioning-of-application-ready-amazon-eks-clusters.html)
from the AWS Solutions Library, extended with EKS Managed Capabilities,
GPU-optimized Karpenter NodePools, and the self-service AI platform layer.

## License

This sample is licensed under **MIT-0** (see [LICENSE](LICENSE)). It does not
vendor any third-party source; all third-party components are pulled at deploy
time from their official registries/charts and remain under their own licenses.
See [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) for attribution.

> Although this repository is released under the MIT-0 license, its chat-UI
> component uses the third-party Open WebUI project. The Open WebUI project's
> licensing includes the Open WebUI License (a modified BSD-3-Clause with a
> branding-retention clause). It is pulled at runtime by image reference and is
> not part of this sample's source; operators who deploy the sample pull that
> image and are subject to its terms.
