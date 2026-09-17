# Self-Service Inference Platform on Amazon EKS

**Run every AI model your teams need - one API, your AWS account!**
  
This sample solution provides a self-service platform that lets teams use large language models the way they ship code: commit a few lines of YAML, git push, and the platform handles the rest - GPUs, serving, scaling, routing, and monitoring. Use frontier **models from Amazon Bedrock** out of the box with no GPUs to manage or **deploy any open-source models**, provisioned and served automatically.
  
And it all runs in your AWS account and desired AWS region - including the AWS European Sovereign Cloud - so your data and models stay where you control them.
  
Key Features:
  
- One API for every model. A single OpenAI-compatible endpoint fronts both Amazon Bedrock and your own Hugging Face / fine-tuned
models - switch models by changing one string, not your code. Use in your IDE/tooling of choice e.g. Cline etc.
- Production-ready by design, with dashboards and tooling out-of the box (Langfuse, Grafana, ArgoCD, LitellmUI, Open WebUI)
- Governance at scale. Each team gets its own API key, budget, and rate limits, with per-user cost tracking and
request tracing built in - so you can safely adjust it to your organizational needs.
- Ship models like code. Adding, updating, or removing a model is a YAML commit that ArgoCD deploys - no consoles, no tickets, no
bespoke infra scripts.
- The hard GPU parts, handled. Right-sizing, autoscaling, multi-GPU parallelism, and scale-out routing come from a few reusable
templates, so you get production-grade serving from a short spec instead of deep Kubernetes/vLLM expertise.

**Stack:** Amazon EKS (ArgoCD · KRO) · AWS ALB · Karpenter · vLLM · LiteLLM · Open WebUI · Langfuse - with an optional **llm-d + Gateway API Inference Extension** scale tier.

## Architecture

**Stack at a glance:** Amazon EKS (ArgoCD · KRO) · AWS ALB · Karpenter · vLLM · LiteLLM · Open WebUI · Langfuse - with an optional **llm-d + Gateway API Inference Extension** scale tier.

Baseline EKS Cluster via terraform as per [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://github.com/aws-solutions-library-samples/guidance-for-automated-provisioning-of-application-ready-amazon-eks-clusters) solution.

![Architecture](docs/img/architecture.png)

```
git push → ArgoCD syncs → KRO expands your YAML into K8s + AWS resources
         → Karpenter provisions a GPU node → vLLM loads the model
         → LiteLLM registers it → available via API, Open WebUI, and Langfuse
```

The custom resources **are** the self-service interface:

| Resource | What it does |
|---|---|
| **`VLLMEndpoint`** | Serve a model on vLLM - the simple default: one model, one pod, one instance (any Hugging Face model ID) |
| **`LLMDEndpoint`** | Serve a model on the llm-d scale tier - KV-cache/load/prefix-aware routing across replicas (the `inference-gateway` substrate ships on every cluster; no toggle) |
| **`LLMDDisaggEndpoint`** | Serve on the llm-d scale + performance tier - independently autoscaled prefill/decode pools (same llm-d substrate; no toggle) |
| **`AITeam`** | Onboard a team: namespace, RBAC, budget, rate limits, scoped API key |

Bedrock models need no resource - they're a few lines of LiteLLM config (`litellm.yaml`), live the
moment the cluster is up. KRO definitions live in `platform/config/kro/`; extend
them there and every model/team inherits the change.

Every model answers through the same LiteLLM `/v1` API, so governance, budgets, and
tracing apply uniformly - including the optional **llm-d** scale tier
(`LLMDEndpoint` / `LLMDDisaggEndpoint`), which LiteLLM forwards to internally.


### Custom Dashboard

![Cluster dashboard - live topology of nodes, GPU slots, and deployed models](docs/img/cluster-dashboard.png)

## Prerequisites

**Tools** (on the machine you run `./platformctl` from):
- **AWS CLI v2** with credentials configured (`aws sts get-caller-identity` must work), 
- [AWS Session Manager plugin for the AWS CLI](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (for `./platformctl tunnel`)
- **Terraform**, **kubectl**, **make**, **jq**, **git**, and **python3** with **boto3**

**AWS account setup**:
- (Optional) If using **Amazon Bedrock models**. Enable desired model(s) in the AWS console and specify model id(s) in `litellm.yaml`.
- (Optional) For any **self deployed model**, sufficient **service quota** for the GPU instance types you plan to self-host on (not needed for the Bedrock-only path)
- (Optional) If using EKS Managed Capabilities (`eks_capabilities = true`):
  An **IAM Identity Center** instance for managed ArgoCD - its ARN and the SSO user who should get
  ArgoCD admin go in the tfvars (`argocd_idc_instance_arn`, `argocd_idc_region`,
  and `argocd_rbac_mappings`). **Its region can differ from your deploy `region`**
  - Identity Center is one instance per account (often in a different region than
  where you deploy this platform), so set `argocd_idc_region` to *that* region, not
  necessarily your deploy region. Find your instance + a user id with:
  ```bash
  # Instance ARN + region (try the regions you may have enabled it in):
  for r in us-east-1 us-west-2 eu-west-1 eu-central-1; do \
    aws sso-admin list-instances --region $r \
      --query 'Instances[].[InstanceArn,IdentityStoreId]' --output text; done
  # SSO user id for argocd_rbac_mappings (use the IdentityStoreId + region above):
  aws identitystore list-users --identity-store-id <d-xxxx> --region <idc-region> \
    --query 'Users[].[UserName,UserId]' --output text
  ```

## Quick start

> ⚠️ **Before you deploy - this creates real, billable infrastructure in your AWS
> account.** It provisions an EKS cluster and (on demand) GPU nodes. The platform
> UIs sit behind an **internal ALB** by default (no public IP) - reach them via
> `./platformctl tunnel` or the opt-in CloudFront edge. If you switch the ALB to
> **internet-facing**, restrict it to your own IP ranges via the **IP allowlist**
> first - never leave it open to the public internet (`0.0.0.0/0`). GPU nodes and
> the cluster incur significant cost; use [Cleanup](#cleanup) to remove
> everything when finished. See [SECURITY.md](SECURITY.md).

0. Create a **GitHub fork of this repo** that ArgoCD can read later - its URL goes in `gitops_repo_url`. Next, clone the forked repo to your local machine
```bash
git clone https://github.com/YOUR-ORG/REPLACE-WITH-YOUR-REPO.git
```

1. Configure: Copy the template, then set your gitops repo URL, and region.
```bash
cd terraform/00.global/vars && cp example.tfvars dev.tfvars   
# edit dev.tfvars - fill every REPLACE marker e.g. `gitops_repo_url` (your fork), `region` and `cluster_endpoint_public_access_cidrs` (your operator IP/CIDR - **required**)
# In case of using EKS Managed Capabilities `eks_capabilities = true`: Your Identity Center ARN + **its region** (`argocd_idc_region`, may differ from `region`) + your **SSO user id** (`argocd_rbac_mappings`), 
```

2. Optional for use with Bedrock, adjust `litellm.yaml`, and update model_list:

```yaml
      - model_name: opus-4-8
        litellm_params:
          model: bedrock/global.anthropic.claude-opus-4-8
          aws_region_name: os.environ/AWS_REGION
```

For AWS European Sovereign Cloud (ESC)
```yaml
      - model_name: nova-lite
        litellm_params:
          model: bedrock/amazon.nova-lite-v1:0
          aws_region_name: os.environ/AWS_REGION
```

3. Provision everything (VPC → EKS + capabilities → Karpenter → secrets).
```bash
#    platformctl reads `region` from dev.tfvars and pins AWS_REGION for you.
./platformctl up dev
```

4. Test - no GPUs yet (up already pointed kubectl at the new cluster)
```bash
./platformctl tunnel        # forward the UIs (WebUI / LiteLLM / Langfuse / Grafana / ArgoCD)
./platformctl status --check  # verify Bedrock + models answer AND Langfuse tracing works
```

5. Deploy a self-hosted model with one command. 
```bash
# `new-model` right-sizes it and ships it end to end:
#      - reads the model's config from Hugging Face and computes its VRAM +
#        tensor-parallelism needs;
#      - picks a cost-ranked GPU instance type allowed by your Karpenter NodePools;
#      - scaffolds the matching endpoint CRD - VLLMEndpoint, or an LLMDEndpoint /
#        LLMDDisaggEndpoint for the llm-d scale tier (--tier, else auto-selected);
#      - with --deploy: writes it to workloads/models/inference/<name>.yaml, then
#        git commits + pushes and nudges ArgoCD to sync.
#    ArgoCD applies it -> Karpenter provisions a GPU node -> vLLM loads the model
#    -> LiteLLM registers it on the /v1 API.
./platformctl new-model Qwen/Qwen2.5-3B-Instruct --deploy   # add -y to skip the confirm prompt
kubectl get vllmendpoints -n inference -w                   # watch it come up (GPU cold start ~ a few min)
```

Drop `--deploy` to just print the recommendation and the ready-to-commit YAML for review (nothing is pushed). 

Size for your traffic with `--seq`, `--users`, or `--workload`, force a serving tier with `--tier`, or point at a private/gated model
with `--hf-token`. See `./platformctl new-model --help` for all flags. 

Example with fine tuning
```bash
./platformctl new-model Qwen/Qwen3.8-27B-FP8 --tp 8 --seq 32768 --quant fp8 \
  --instance-type g6.48xlarge --worker-memory 120Gi \
  --tool-call-parser qwen3_xml \
  --extra-arg '["--reasoning-parser","qwen3","--kv-cache-dtype","fp8","--enable-prefix-caching"]' \
  --deploy \
  --hf-token <your-hf-token>
```

> ⚠️ **This might take 10-15 minutes to complete, until endpoint is ready!**

> ⚠️ **The recommended instance type is a sizing guide, not a guarantee.** It's
> computed from the model's memory footprint and current on-demand pricing. What
> Karpenter actually launches is decided at provisioning time from *real* capacity:
> it picks a compatible type from the GPU NodePool's allowed set based on what's
> available in your region/AZs at that moment - so the node you get (and its
> hourly cost) may differ from the recommendation. The model still fits and serves
> correctly; only the specific instance may vary. To override the selection, you may specify  `--instance-type`.

6. Removing a model 

Folder `workloads/models/inference`
```bash
./platformctl new-model <yaml-name> --undeploy
```

## Credentials

- Open Web UI, uses Cognito, see Terraform Output
- Custom Dashboard, uses Cognito, see Terraform Output
- Langfuse, see Terraform Output
- LiteLLM UI user: admin, password see Terraform Output
```bash
kubectl -n ai-platform get secret litellm-secrets -o jsonpath='{.data.master-key}' | base64 -d
```
- ArgoCD (without Identity Center) user:admin, password
```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

## AWS European Sovereign Cloud

Check `example-esc.tfvars` for a working deployment config!

Known Issues

- Public ECR Repo equivalent not available. Karpenter, ACK, aws-application-networking (LB/Gateway), eks-distro will be loaded from `public.ecr.aws`.
- Requires NAT Gateway
- EKS Managed Capability not available - add-ons will be installed via Helm automatically instead `eks_capabilities = false`
- Bedrock: Limited models available, make sure to adjust `litellm.yaml`

## Beyond the basics

**Serve a fine-tuned model.** Any HuggingFace model ID works - including a model
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
model-weight cache - pre-seed a model's HuggingFace weights there and the serving
initContainer loads them from local disk instead of pulling from HuggingFace. Actual
savings vary by model and instance.

**Platform Health Agent.** The cluster dashboard can watch for failures, investigate
them with an LLM, and propose a one-click fix - idle until you provide a Kiro key.
See **[its guide](platform/services/cluster-dashboard/PLATFORM-HEALTH-AGENT.md)**.

**Cost control.** Karpenter right-sizes and consolidates GPU nodes to match demand
and reclaims them when workloads are removed; `shared: true` time-slices one
physical GPU across up to 4 small models. The cluster dashboard's **Cost** view adds
a **tokens-per-dollar** efficiency leaderboard per model - combining live LiteLLM
throughput with the GPU/Bedrock cost basis the dashboard already tracks - so the
least cost-efficient models (and idle-but-billing ones) surface at a glance.

**Staying current (EKS addons).** All EKS managed addons (vpc-cni, CoreDNS,
kube-proxy, pod-identity, EBS CSI, metrics-server) are declared `most_recent = true`
in Terraform, so `terraform apply` converges them to the latest version. To catch a
version falling behind *between* applies, a weekly **report-only** GitHub Action
([`.github/workflows/addon-freshness.yaml`](.github/workflows/addon-freshness.yaml))
checks each managed addon against the newest available and opens a GitHub issue with
the exact `update-addon` commands when any is behind - it never mutates the cluster.
It's scoped to EKS **managed addons only**; OSS components (LiteLLM, Open WebUI,
Langfuse, etc.) stay operator-pinned. Enable it by setting the `ADDON_RECONCILER_*`
repo variables (see the workflow header).

**Team self-service (GitOps).** Onboard a team with an `AITeam` YAML in
`workloads/teams/` - it creates a `team-<name>` namespace with a GPU quota, RBAC,
namespace isolation (a default-deny **ingress** NetworkPolicy - only same-team and
platform namespaces can reach in), and a scoped LiteLLM key (budget + rpm/tpm).
Scaffold that YAML with `./platformctl onboard-team <name> [--gpu N --budget USD
--models a,b ...]` - it mirrors `new-model`: prints the manifest for review by
default, and with `--deploy` writes it, commits, and pushes so ArgoCD applies it.
The team then
deploys models by committing a `VLLMEndpoint` under **`workloads/models/team-<name>/`**
- the directory name is the target namespace, so models land in that team's quota
and key (no `kubectl`, no console; removal is `git rm`). By default workloads live
in this repo; for real multi-team self-service point `gitops_workloads_repo_url` at
a separate, tenant-owned repo so teams get write access to the workloads repo only,
never the platform repo. See [`workloads/models/README.md`](workloads/models/README.md).

**Single sign-on, per-user cost & budgets.** SSO ships enabled (`enable_sso`,
default on): Terraform stands up an **Amazon Cognito** user pool with a hosted login
page, role groups (`admins`/`developers`/`users`), and three seed users - log in
by email (`admin@example.com` / `developer@example.com` / `user@example.com`),
whose generated passwords are the `sso_seed_user_passwords` Terraform output -
retrieve them any time (the `up` output scrolls away) with:

```bash
TF_WORKSPACE=<env> terraform -chdir=terraform/30.eks/30.cluster output -json sso_seed_user_passwords
```

Open WebUI, the LiteLLM admin UI, and Langfuse all federate to Cognito, and Open
WebUI forwards the signed-in identity so **cost is attributed per user** in
LiteLLM's spend reports.
LiteLLM also enforces a **default per-user budget + rpm/tpm throttle** on the chat
path (from that forwarded identity, no keys needed) and caps any API key a user
mints for themselves - tune the defaults in
[`platform/services/litellm/litellm.yaml`](platform/services/litellm/litellm.yaml).
It works out of the box over `./platformctl tunnel` (Cognito allows `localhost`
callbacks); bring your own enterprise IdP by federating it into the pool. Cognito is
the only new hard dependency - Identity Center stays required only for ArgoCD SSO.
For **public HTTPS** access (and to protect the dashboard behind auth), opt in to
the CloudFront edge with `./platformctl edge cloudfront` - Terraform stands up a
CloudFront **VPC origin** to the private ALB, with a free `*.cloudfront.net`
certificate (no domain needed) and the Cognito callbacks wired automatically. For
your own domain, `./platformctl edge domain` (internet-facing ALB + your ACM cert).
See **[docs/cloudfront-edge.md](docs/cloudfront-edge.md)** for how it works and the
edge gotchas the Terraform handles.

---

## Cleanup

```bash
./platformctl down dev         # for dev.tfvars
```

Walks the six terraform stages in reverse
(`oss-obs → native-obs → addons → cluster → iam → networking`). On a healthy
cluster that has been idle, it finishes in ~25 minutes. On a cluster that's
been actively running models, expect **30–45 min** and a few hand-cleanup
steps below. The script does **not** touch the bootstrap state (S3
`tfstate-<account>` + DynamoDB `tfstate-lock`), so a subsequent
`./platformctl up <env>` still works.

### Required manual cleanup steps

* **`Error acquiring the state lock`** - a previous `terraform` run was
  killed mid-flight. Find the lock ID in the error and run
  `terraform -chdir=terraform/<stage> force-unlock -force <id>`.
* **`kubernetes_namespace.ai_platform: Still destroying...`** for many
  minutes - a `NetworkPolicy` finalizer (`networking.k8s.aws/resources`)
  is waiting for the VPC CNI controller, which has already been destroyed.
  Strip the finalizers manually:
  `kubectl get networkpolicies -n ai-platform -o name | xargs -I {} kubectl patch -n ai-platform {} --type=merge -p '{"metadata":{"finalizers":[]}}'`
* **`ECR Repository ... not empty`** - empty it with `aws ecr batch-delete-image`, then re-run.
* **Subnet stuck destroying for 15+ min** - almost always an orphan
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
platformctl         The unified CLI (use · up · status · tunnel · edge · new-model · onboard-team · down · list-envs)
ops/                platformctl implementation: ops/lib/ (helpers) · ops/image/ (cold-start build helpers)
terraform/          Infrastructure modules (VPC → IAM → EKS → observability)
docs/               cloudfront-edge
.github/workflows/  ci (pre-merge validation) · addon-freshness (weekly report-only EKS-addon drift check)
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
