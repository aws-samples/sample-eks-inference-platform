apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: bootstrap
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: ai-platform
    ai-platform/managed-by: terraform
spec:
  project: default
  source:
    repoURL: ${repo_url}
    targetRevision: ${revision}
    path: argocd/bootstrap
    # argocd/bootstrap is a Helm chart (app-of-apps). Pass the repo URL(s) as
    # Helm values from the SAME Terraform vars used above, so the platform +
    # workloads ApplicationSets inherit them — the repo URL is set in exactly
    # one place (tfvars). workloadsRepoURL empty -> workloads live in this repo.
    helm:
      valuesObject:
        repoURL: ${repo_url}
        revision: ${revision}
        workloadsRepoURL: ${workloads_repo_url}
  destination:
    name: local-cluster
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
