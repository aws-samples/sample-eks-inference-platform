"""Tests for the EKS managed-addon freshness detector (ops/lib/addon_freshness.py).

The detector is the weekly report-only drift check. Its correctness hinges on two
pure pieces — the version ordering (what counts as "behind") and the markdown it
puts in the GitHub issue — so those are unit-tested here without any AWS calls.
"""

import addon_freshness as af


class TestVersionKey:
    def test_patch_and_eksbuild_ordering(self):
        # Real versions from the ai-platform-e2e incident.
        assert af._version_key("v1.62.0-eksbuild.1") < af._version_key("v1.65.0-eksbuild.1")
        # eksbuild number breaks ties within the same semver.
        assert af._version_key("v1.14.3-eksbuild.3") < af._version_key("v1.14.3-eksbuild.14")
        assert af._version_key("v1.36.0-eksbuild.13") < af._version_key("v1.36.0-eksbuild.17")

    def test_missing_or_odd_shapes_do_not_raise(self):
        assert af._version_key(None) == (0, 0, 0, 0)
        assert af._version_key("") == (0, 0, 0, 0)
        # No eksbuild suffix -> build component 0.
        assert af._version_key("v1.4.0") == (1, 4, 0, 0)

    def test_newest_picks_highest(self):
        versions = ["v1.62.0-eksbuild.1", "v1.65.0-eksbuild.1", "v1.63.1-eksbuild.1"]
        assert af._newest(versions) == "v1.65.0-eksbuild.1"
        assert af._newest([]) is None


def _report(drift):
    rows = [
        {"name": "aws-ebs-csi-driver", "current": "v1.62.0-eksbuild.1",
         "latest": "v1.65.0-eksbuild.1", "behind": drift, "status": "ACTIVE"},
        {"name": "coredns", "current": "v1.14.3-eksbuild.14",
         "latest": "v1.14.3-eksbuild.14", "behind": False, "status": "ACTIVE"},
    ]
    return {"cluster": "ai-platform-e2e", "region": "us-east-1", "k8sVersion": "1.36",
            "addons": rows, "driftCount": 1 if drift else 0}


class TestRenderMarkdown:
    def test_drift_body_has_runbook_and_details(self):
        md = af.render_markdown(_report(drift=True))
        assert "aws-ebs-csi-driver" in md
        assert "v1.65.0-eksbuild.1" in md
        assert "update-addon" in md
        assert "PRESERVE" in md          # preserves existing config
        assert "**1**" in md             # drift count surfaced

    def test_clean_body_has_no_commands(self):
        md = af.render_markdown(_report(drift=False))
        assert "update-addon" not in md
        assert "latest available version" in md

    def test_human_render_flags_behind(self):
        txt = af.render_human(_report(drift=True))
        assert "BEHIND" in txt
        assert "1 addon(s) behind latest." in txt
