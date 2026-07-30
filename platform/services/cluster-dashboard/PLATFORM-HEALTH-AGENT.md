# Platform Health Agent

> Internal Kubernetes resources keep the name `platform-health-agent` for backwards
> compatibility. The public-facing display name was changed to avoid a clash
> with AWS's "Platform Health Agent" service. The ServiceAccounts, ClusterRoles,
> and the postgres database are all still named `platform-health-agent` / `platform_health_agent`.

> **Packaging:** the agent ships as a **component of the cluster-dashboard app**,
> not as a standalone service. Its manifests live alongside the dashboard's
> (`platform/services/cluster-dashboard/pha-*.yaml`, wired via that directory's
> `kustomization.yaml`) and **everything runs in the `ai-platform` namespace** —
> no dedicated namespace, no separate ArgoCD Application.

EKS-native platform capability. Watches the cluster for actionable
incidents (CrashLoopBackOff, OOMKilled, FailedScheduling, NodeNotReady, …),
investigates with `kiro-cli`, surfaces the proposed fix in the existing
**cluster-dashboard** with `[Approve]` / `[Dismiss]` buttons, and applies
the fix only after you click Approve.

The agent's `event-watcher` Deployment is **always synced** with the dashboard,
but **idles gracefully** until a Kiro API key is present: with no key it boots,
serves a healthy endpoint, and watches nothing (no investigations, no Jobs), so a
default cluster stays green. Creating the key Secret (one `kubectl` command,
below) activates it — no Terraform, no GitOps change.

> See [`docs/platform-health-agent-architecture-design.md`](../../../docs/platform-health-agent-architecture-design.md)
> and [`docs/platform-health-agent-implementation-plan.md`](../../../docs/platform-health-agent-implementation-plan.md)
> for the full design.

---

## Approvals UX

Approvals happen entirely **inside the cluster-dashboard** (`http://<alb>:9090`,
already restricted by IP allowlist). When the agent finishes an investigation,
the dashboard's topbar shows a `🔔 N pending` badge. Click it to open a panel
listing each proposed fix with its severity, root cause, fix commands, and
`[Approve]` / `[Dismiss]` buttons.

**No external messaging system required** — no Slack app, no email, no domain
or HTTPS setup. Same security model as the dashboard (ALB inbound CIDR
allowlist).

---

## Enable / disable

The agent's manifests are **always deployed** (they're part of the always-synced
cluster-dashboard app). What gates it is a single Secret holding your Kiro API
key — exactly like the `hf-token` Secret for gated HuggingFace models, it's an
external credential you create with `kubectl`, not Terraform.

**Enable** — create the Secret + restart the watcher to pick it up:

```bash
kubectl create secret generic platform-health-agent-secrets \
  -n ai-platform --from-literal=KIRO_API_KEY='kr-xxxxxxxxxxxxxxxxxxxxxxxx'
kubectl rollout restart deployment event-watcher -n ai-platform
```

The event-watcher reads `KIRO_API_KEY` (mounted `optional: true`); present → it
activates and begins investigating; absent → it idles. The DB credentials it
needs (`platform-db-credentials`) already exist in `ai-platform`, so the pod
never CrashLoops either way. No `terraform apply`, no tfvar, no ApplicationSet
edit.

**Disable** — delete the Secret + restart:

```bash
kubectl delete secret platform-health-agent-secrets -n ai-platform
kubectl rollout restart deployment event-watcher -n ai-platform
```

It returns to idle. The `platform_health_agent` postgres database persists
(intentional — re-enabling resumes from the same state), and the dashboard's
approvals UI hides the badge once no investigations are pending.

---

## One-time setup

### 1. Get the Kiro API key

1. <https://kiro.dev/> → log in → API keys → create a key with headless-mode scope.
2. Copy the key value (one-time view).

### 2. Create the Secret + activate the watcher

```bash
kubectl create secret generic platform-health-agent-secrets \
  -n ai-platform --from-literal=KIRO_API_KEY='kr-xxxxxxxxxxxxxxxxxxxxxxxx'
kubectl rollout restart deployment event-watcher -n ai-platform

# Verify the Secret landed:
kubectl -n ai-platform get secret platform-health-agent-secrets \
  -o jsonpath='{.data}' | python3 -c "import sys,json; print(list(json.loads(sys.stdin.read()).keys()))"
# Expected: ['KIRO_API_KEY']

kubectl get pods -n ai-platform -l app.kubernetes.io/name=event-watcher -w
```

No Slack tokens, no signing secrets, no domain — the only secret you supply is
the Kiro API key (the DB credentials already live in `ai-platform`).

Expected:
1. `event-watcher-…` Pod comes up Ready 1/1 within 60s and logs
   `event-watcher started: …` (instead of the `Platform Health Agent DISABLED` idle line).
2. cluster-dashboard auto-detects investigations; refresh `http://<alb>:9090` and the topbar shows `🔔 0 pending`.

> **Tip:** to keep the key out of shell history / make it reproducible, you can
> store it in AWS Secrets Manager and sync it with an `ExternalSecret` (the
> External Secrets Operator is installed) instead of `kubectl create secret`.

---

## Architecture

```
                        ┌────────────────────────┐
                        │  event-watcher (1)     │
                        │  Watches K8s API       │ — platform-health-agent-reader SA
                        │  Spawns Investigator   │   (cluster-wide get/list/watch)
                        └────────────┬───────────┘
                                     │ creates Job
                                     ▼
                        ┌────────────────────────┐
                        │ Investigator Job       │
                        │ kiro-cli + kubectl     │ — platform-health-agent-reader SA
                        │ → /results/findings.   │   (read-only, RBAC-enforced)
                        │   json                 │
                        └────────────┬───────────┘
                                     │ persist_findings.py investigation
                                     ▼
                        ┌────────────────────────┐
                        │ platform_health_agent DB        │ ◄── poll every 2s
                        │  (on platform-db-0)    │     by cluster-dashboard
                        └────────────────────────┘     backend
                                     ▲
                                     │ click [Approve]
                                     │ in cluster-dashboard topbar
                        ┌────────────┴───────────┐
                        │ cluster-dashboard      │ — cluster-dashboard SA
                        │ topbar approvals UI    │   + create-jobs in
                        │ POST /investigations/  │     ai-platform ns
                        │   <id>/approve         │
                        │ Spawns Remediator      │
                        └────────────┬───────────┘
                                     │ creates Job
                                     ▼
                        ┌────────────────────────┐
                        │ Remediator Job         │ — platform-health-agent-writer SA
                        │ kiro-cli + kubectl     │   (RoleBinding scoped to:
                        │ Applies, verifies,     │    `inference` and each
                        │ → /results/result.json │    `team-*` namespace)
                        └────────────┬───────────┘
                                     │ persist_findings.py remediation
                                     ▼
                        ┌────────────────────────┐
                        │ platform_health_agent DB        │
                        │ status='done'          │
                        └────────────────────────┘
```

---

## Configuration

All knobs are in [`pha-config.yaml`](./pha-config.yaml). Most useful:

| Key | Effect |
|-----|--------|
| `WATCH_NAMESPACES` | `*` = all (minus excludes), or comma list |
| `EXCLUDE_NAMESPACES` | Never investigate events in these namespaces |
| `TRIGGER_*` | Toggle each trigger type (`true`/`false`) |
| `MAX_CONCURRENT_INVESTIGATIONS` | Cluster-wide cap (default 3) |
| `MAX_INVESTIGATIONS_PER_DAY` | Daily budget (default 50) |
| `MAX_REMEDIATIONS_PER_DAY` | Daily budget (default 20) |
| `KIRO_MODEL_INVESTIGATE` | Model used for investigation prompts |
| `KIRO_MODEL_REMEDIATE` | Model used for remediation prompts |
| `APPROVAL_EXPIRY_HOURS` | Approve buttons disabled after this |
| `DEBOUNCE_WINDOW_SEC` | Suppress repeat investigations for the same resource |

Editing the ConfigMap and pushing triggers a kustomize rehash → automatic pod restart. In-flight Jobs keep their original env.

---

## Operations

### Read the audit trail

```bash
# All investigations, newest first:
kubectl exec -n ai-platform platform-db-0 -- \
  psql -U platform -d platform_health_agent -c \
  "SELECT id, created_at, status, trigger_kind, resource_namespace, resource_name FROM investigations ORDER BY created_at DESC LIMIT 20"

# Today's counters:
kubectl exec -n ai-platform platform-db-0 -- \
  psql -U platform -d platform_health_agent -c "SELECT * FROM today_counters"

# Pending approvals (also visible in the dashboard):
kubectl exec -n ai-platform platform-db-0 -- \
  psql -U platform -d platform_health_agent -c \
  "SELECT id, trigger_kind, resource_namespace, resource_name, created_at FROM investigations WHERE status='awaiting_approval'"
```

### Pause without disabling

```bash
kubectl scale deployment event-watcher -n ai-platform --replicas=0
# Resume:
kubectl scale deployment event-watcher -n ai-platform --replicas=1
```

ArgoCD will revert this within ~3 min because of `selfHeal: true`. For a longer pause, set all `TRIGGER_*=false` in the ConfigMap and push.

### Inspect a running investigation

```bash
kubectl get jobs -n ai-platform
kubectl logs -n ai-platform job/investigator-<8-char-hex>
```

---

## Troubleshooting

### "Dashboard topbar doesn't show the approvals badge"

1. `kubectl logs -n ai-platform deploy/cluster-dashboard --tail=50` — should show successful `db_health` polls. If `connection refused` or `database does not exist`, the watcher's `db-init` initContainer didn't run.
2. `kubectl logs -n ai-platform deploy/event-watcher -c db-init` — the DB-provisioning initContainer; check it completed without error.
3. `kubectl logs -n ai-platform deploy/event-watcher --tail=20` — should log `event-watcher started: …`. If it logs `Platform Health Agent DISABLED — no KIRO_API_KEY`, the agent is idle (no Kiro key — see Enable / disable).

### "The agent investigated something silly"

- Tighten `EXCLUDE_NAMESPACES`: add the namespace.
- Toggle off the noisy trigger via `TRIGGER_*=false`.
- If a specific resource keeps re-triggering: check the `debounce` table:
  ```sql
  SELECT * FROM debounce ORDER BY last_seen DESC LIMIT 10;
  ```

### "Approve button does nothing"

- Open browser dev tools → Network tab → click Approve again. The POST should return 200.
- Possible non-200 reasons:
  - `409 expired` — investigation older than `APPROVAL_EXPIRY_HOURS`.
  - `429 budget exceeded` — daily remediation budget hit.
  - `503 db unavailable` — postgres/agent connectivity broken.

### "Remediator gets 403"

- The target namespace doesn't have a `platform-health-agent-writer` RoleBinding.
- The reconciler runs every 5 min for `team-*` namespaces. If you just created a team and clicked Approve before the next sweep, the binding doesn't exist yet.
- Force a sweep: `kubectl rollout restart deployment event-watcher -n ai-platform`.
- Check: `kubectl get rolebinding platform-health-agent-writer -n team-yourteam -o yaml`.

---

## Cost notes

The agent uses Kiro CLI hosted models:
- Investigations: `claude-sonnet-4.6` (1.30x credits) — typically 5-30K tokens per run
- Remediations: `claude-opus-4.6` (2.20x credits) — typically 5-15K tokens per run

With the default budgets, upper bound ~2.6M weighted tokens/day. Adjust `KIRO_MODEL_*` to switch models (e.g. `claude-haiku-4.5` for cheaper investigations).

---

## Future work

- ESO-backed secret rotation
- `awslabs.eks-mcp-server` MCP wiring (V1 uses raw kubectl)
- Stronger approver auth on the dashboard (basic auth / OIDC)
- Webhook-style remediation that opens a PR instead of applying imperatively
- ARM64 image variants
- Prometheus metrics + Grafana dashboard
