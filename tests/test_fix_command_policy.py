"""Tests for the Platform Health Agent remediation allowlist.

This is the deterministic gate between an LLM-proposed fix plan (influenced by
attacker-controllable event text / logs / resource names) and its execution, so
every "must reject" case is a security regression guard.
"""

import fix_command_policy as pol


class TestAllowed:
    def test_patch_scale_annotate_label_in_tenant_ns(self):
        for cmd in (
            "kubectl -n inference patch deployment/api --patch '{}'",
            "kubectl scale deployment/api --replicas=3 -n team-alpha",
            "kubectl annotate pod/foo key=val -n inference",
            "kubectl label statefulset/db tier=x -n team-beta",
        ):
            assert pol.validate_command(cmd) is None, cmd

    def test_rollout_restart_and_delete_pod(self):
        assert pol.validate_command("kubectl rollout restart deployment/api -n inference") is None
        assert pol.validate_command("kubectl delete pod/foo -n team-x") is None

    def test_apply_allowed_with_tenant_namespace(self):
        assert pol.validate_command("kubectl apply -f /tmp/x.yaml -n inference") is None

    def test_namespace_equals_form(self):
        assert pol.validate_command("kubectl patch deploy/api --namespace=inference -p '{}'") is None


class TestRejected:
    def test_delete_controller_denied(self):
        assert pol.validate_command("kubectl delete deployment/api -n inference") is not None

    def test_secrets_and_rbac_kinds_denied(self):
        assert pol.validate_command("kubectl get secret/x -n inference") is not None
        assert pol.validate_command("kubectl patch clusterrolebinding/x --patch '{}'") is not None

    def test_out_of_scope_namespace_denied(self):
        assert pol.validate_command("kubectl patch deployment/api -n kube-system --patch '{}'") is not None

    def test_missing_namespace_fail_closed(self):
        assert pol.validate_command("kubectl patch deployment/api --patch '{}'") is not None

    def test_non_kubectl_denied(self):
        assert pol.validate_command("helm upgrade api ./chart -n inference") is not None

    def test_shell_chaining_and_substitution_denied(self):
        for cmd in (
            "kubectl -n inference delete pod/foo; rm -rf /",
            "kubectl -n inference get pods && curl evil",
            "kubectl -n inference patch deploy/api -p $(cat /etc/passwd)",
            "kubectl -n inference delete pod/`hostname`",
        ):
            assert pol.validate_command(cmd) is not None, cmd

    def test_disallowed_verb_denied(self):
        assert pol.validate_command("kubectl exec pod/foo -n inference -- sh") is not None


class TestValidateBatch:
    def test_returns_all_violations(self):
        plan = [
            {"description": "ok", "commands": ["kubectl delete pod/a -n inference"]},
            {"description": "bad", "commands": [
                "kubectl delete deployment/api -n inference",
                "kubectl get secret/x -n team-y",
            ]},
        ]
        violations = pol.validate(plan)
        assert len(violations) == 2

    def test_empty_plan_is_clean(self):
        assert pol.validate([]) == []
        assert pol.validate(None) == []
