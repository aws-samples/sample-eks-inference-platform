"""Scoping-safety tests for the post-teardown orphan sweep.

sweep_env deletes real AWS resources, so its one non-negotiable invariant is:
it must NEVER act on a resource that isn't clearly part of THIS cluster, even
when another environment/project shares an `Environment` tag value. These tests
drive each destructive path with fake AWS clients and assert exactly which
resources get mutated — an over-broad match here is a data-loss bug.
"""

import pytest

import sweep_env

CLUSTER = "ai-platform-dev"
ENV = "dev"
REGION = "us-west-2"


@pytest.fixture
def sweeper(monkeypatch):
    # Don't build real boto3 clients; the test overrides the ones it exercises.
    monkeypatch.setattr(sweep_env.boto3, "client", lambda *a, **k: object())
    return sweep_env.Sweeper(ENV, CLUSTER, REGION)


# --------------------------------------------------------------------------- #
# _has_cluster_tag — the core "is this ours?" predicate                       #
# --------------------------------------------------------------------------- #
class TestHasClusterTag:
    def test_true_for_cluster_ownership_tags(self, sweeper):
        assert sweeper._has_cluster_tag([{"Key": f"kubernetes.io/cluster/{CLUSTER}", "Value": "owned"}])
        assert sweeper._has_cluster_tag([{"Key": "eks:cluster-name", "Value": CLUSTER}])
        assert sweeper._has_cluster_tag([{"Key": "karpenter.sh/discovery", "Value": CLUSTER}])
        assert sweeper._has_cluster_tag([{"Key": "Name", "Value": f"{CLUSTER}-nat"}])

    def test_false_without_a_clear_cluster_tie(self, sweeper):
        assert not sweeper._has_cluster_tag([])
        assert not sweeper._has_cluster_tag(None)
        # Different cluster value must not match.
        assert not sweeper._has_cluster_tag([{"Key": "eks:cluster-name", "Value": "other-cluster"}])
        assert not sweeper._has_cluster_tag([{"Key": "kubernetes.io/cluster/other", "Value": "owned"}])

    def test_environment_tag_alone_is_not_sufficient(self, sweeper):
        # Critical: a shared `Environment=dev` tag must NOT authorize destruction
        # via the tag predicate — otherwise the sweep could hit another project.
        assert not sweeper._has_cluster_tag([{"Key": "Environment", "Value": ENV}])


# --------------------------------------------------------------------------- #
# clear_kms — only this cluster's customer-managed, enabled keys               #
# --------------------------------------------------------------------------- #
class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_):
        return iter(self._pages)


class FakeKms:
    def __init__(self, metadata_by_id):
        self._meta = metadata_by_id
        self.scheduled = []

    def get_paginator(self, name):
        assert name == "list_keys"
        return _Paginator([{"Keys": [{"KeyId": k} for k in self._meta]}])

    def describe_key(self, KeyId):
        return {"KeyMetadata": self._meta[KeyId]}

    def schedule_key_deletion(self, KeyId, PendingWindowInDays):
        self.scheduled.append(KeyId)
        return {"DeletionDate": "2026-01-01"}


class TestClearKms:
    def test_schedules_only_in_scope_customer_enabled_keys(self, sweeper):
        sweeper.kms = FakeKms({
            "in-scope":  {"KeyManager": "CUSTOMER", "KeyState": "Enabled",
                          "Description": f"EKS secrets key for {CLUSTER}"},
            "aws-mgd":   {"KeyManager": "AWS", "KeyState": "Enabled",
                          "Description": f"{CLUSTER}"},
            "disabled":  {"KeyManager": "CUSTOMER", "KeyState": "Disabled",
                          "Description": f"{CLUSTER}"},
            "other":     {"KeyManager": "CUSTOMER", "KeyState": "Enabled",
                          "Description": "some other cluster"},
        })
        sweeper.clear_kms()
        assert sweeper.kms.scheduled == ["in-scope"]


# --------------------------------------------------------------------------- #
# clear_eips — release only unassociated, in-scope addresses                   #
# --------------------------------------------------------------------------- #
class FakeEipEc2:
    def __init__(self, addresses):
        self._addresses = addresses
        self.released = []

    def describe_addresses(self):
        return {"Addresses": self._addresses}

    def release_address(self, AllocationId):
        self.released.append(AllocationId)


class TestClearEips:
    def test_scoping_and_in_use_protection(self, sweeper):
        sweeper.ec2 = FakeEipEc2([
            # in use (associated) even though in-scope -> must be left alone
            {"AllocationId": "a-assoc", "AssociationId": "eipassoc-1",
             "Tags": [{"Key": "Name", "Value": f"{CLUSTER}-nat"}]},
            # unassociated + cluster tag -> release
            {"AllocationId": "a-cluster", "PublicIp": "1.1.1.1",
             "Tags": [{"Key": f"kubernetes.io/cluster/{CLUSTER}", "Value": "owned"}]},
            # unassociated + Environment tag -> release (env-scoped orphan)
            {"AllocationId": "a-env", "PublicIp": "2.2.2.2",
             "Tags": [{"Key": "Environment", "Value": ENV}]},
            # unassociated + unrelated -> leave alone
            {"AllocationId": "a-other", "PublicIp": "3.3.3.3",
             "Tags": [{"Key": "Environment", "Value": "prod"}]},
        ])
        sweeper.clear_eips()
        assert sorted(sweeper.ec2.released) == ["a-cluster", "a-env"]


# --------------------------------------------------------------------------- #
# clear_launch_templates — name match OR cluster tag                           #
# --------------------------------------------------------------------------- #
class FakeLtEc2:
    def __init__(self, templates, tags_by_id):
        self._templates = templates
        self._tags_by_id = tags_by_id
        self.deleted = []

    def describe_launch_templates(self):
        return {"LaunchTemplates": self._templates}

    def describe_tags(self, Filters):
        rid = Filters[0]["Values"][0]
        return {"Tags": self._tags_by_id.get(rid, [])}

    def delete_launch_template(self, LaunchTemplateId):
        self.deleted.append(LaunchTemplateId)


class TestClearLaunchTemplates:
    def test_deletes_by_name_or_tag_only(self, sweeper):
        sweeper.ec2 = FakeLtEc2(
            templates=[
                {"LaunchTemplateId": "lt-name", "LaunchTemplateName": f"{CLUSTER}-workers"},
                {"LaunchTemplateId": "lt-tag", "LaunchTemplateName": "karpenter-abc"},
                {"LaunchTemplateId": "lt-other", "LaunchTemplateName": "unrelated"},
            ],
            tags_by_id={
                "lt-tag": [{"Key": "eks:cluster-name", "Value": CLUSTER}],
                "lt-other": [{"Key": "eks:cluster-name", "Value": "other-cluster"}],
            },
        )
        sweeper.clear_launch_templates()
        assert sorted(sweeper.ec2.deleted) == ["lt-name", "lt-tag"]


# --------------------------------------------------------------------------- #
# clear_instance_profiles — name or path scoped                                #
# --------------------------------------------------------------------------- #
class FakeIam:
    def __init__(self, profiles):
        self._profiles = profiles
        self.deleted = []
        self.removed_roles = []

    def get_paginator(self, name):
        assert name == "list_instance_profiles"
        return _Paginator([{"InstanceProfiles": self._profiles}])

    def remove_role_from_instance_profile(self, InstanceProfileName, RoleName):
        self.removed_roles.append((InstanceProfileName, RoleName))

    def delete_instance_profile(self, InstanceProfileName):
        self.deleted.append(InstanceProfileName)


class TestClearInstanceProfiles:
    def test_scopes_by_name_or_path(self, sweeper):
        sweeper.iam = FakeIam([
            {"InstanceProfileName": f"{CLUSTER}-node", "Path": "/", "Roles": [{"RoleName": "r1"}]},
            {"InstanceProfileName": "karpenter", "Path": f"/eks/{CLUSTER}/", "Roles": []},
            {"InstanceProfileName": "unrelated", "Path": "/", "Roles": [{"RoleName": "r2"}]},
        ])
        sweeper.clear_instance_profiles()
        assert sorted(sweeper.iam.deleted) == [f"{CLUSTER}-node", "karpenter"]
        # Roles are detached only from in-scope profiles before deletion.
        assert sweeper.iam.removed_roles == [(f"{CLUSTER}-node", "r1")]
