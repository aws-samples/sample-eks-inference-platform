"""Tests for the workloads-layout SSOT and its safety validators.

These validators are the path-traversal guard for `--undeploy NAME` and the
YAML-injection guard for `--deploy` (a model id/name is interpolated into a
manifest that ArgoCD commits and applies), so their reject cases are security
regressions, not cosmetic ones.
"""

from recommend_instance import paths


class TestIsValidModelName:
    def test_allows_rfc1123_labels(self):
        for name in ("llama-3-1-8b", "qwen3-32b", "a", "m5", "x" * 63):
            assert paths.is_valid_model_name(name), name

    def test_rejects_traversal_and_separators(self):
        for name in ("../etc", "a/b", "..", "./x", "a\\b", "/abs"):
            assert not paths.is_valid_model_name(name), name

    def test_rejects_shape_violations(self):
        # empty, too long, uppercase, leading/trailing dash, spaces, quotes
        # incl. a trailing newline — Python's $ anchor would let "a\n" pass.
        for name in ("", "x" * 64, "Llama", "-lead", "trail-", "a b", 'a"b', "a\n", "a\n b"):
            assert not paths.is_valid_model_name(name), name


class TestIsValidHfModelId:
    def test_allows_org_slash_name_and_bare(self):
        for mid in (
            "meta-llama/Llama-3.1-8B-Instruct",
            "Qwen/Qwen3-32B",
            "google/gemma-3-4b-it",
            "gpt2",
        ):
            assert paths.is_valid_hf_model_id(mid), mid

    def test_rejects_yaml_injection_payloads(self):
        for mid in (
            'org/model"\ninjected: true',   # quote + newline breakout
            "org/model\nfoo: bar",           # newline
            'x"; rm -rf /',                  # quote + shell
            "a b/c",                          # space
            "org/a/b",                        # more than one slash
            "../etc",                         # traversal
            "org/model\n",                    # trailing newline ($-anchor hole)
            "",                               # empty
            "x" * 201,                        # over length cap
        ):
            assert not paths.is_valid_hf_model_id(mid), mid


class TestFindModelFiles:
    def _tree(self, root):
        # inference/ (platform default), team-x/ (tenant), scale-models/ (flat)
        for rel in ("workloads/models/inference", "workloads/models/team-x",
                    "workloads/scale-models"):
            (root / rel).mkdir(parents=True, exist_ok=True)

    def test_finds_inference_placement(self, tmp_path):
        self._tree(tmp_path)
        (tmp_path / "workloads/models/inference/llama.yaml").write_text("{}")
        assert paths.find_model_files(str(tmp_path), "llama") == [
            "workloads/models/inference/llama.yaml"
        ]

    def test_finds_team_placement_recursively(self, tmp_path):
        self._tree(tmp_path)
        (tmp_path / "workloads/models/team-x/qwen.yaml").write_text("{}")
        assert paths.find_model_files(str(tmp_path), "qwen") == [
            "workloads/models/team-x/qwen.yaml"
        ]

    def test_finds_flat_scale_models(self, tmp_path):
        self._tree(tmp_path)
        (tmp_path / "workloads/scale-models/big.yaml").write_text("{}")
        assert paths.find_model_files(str(tmp_path), "big") == [
            "workloads/scale-models/big.yaml"
        ]

    def test_reports_ambiguity_when_name_in_two_places(self, tmp_path):
        # A name deployed in both the inference tree and scale-models must yield
        # BOTH matches so the caller can refuse rather than delete the wrong one.
        self._tree(tmp_path)
        (tmp_path / "workloads/models/inference/dup.yaml").write_text("{}")
        (tmp_path / "workloads/scale-models/dup.yaml").write_text("{}")
        matches = paths.find_model_files(str(tmp_path), "dup")
        assert len(matches) == 2

    def test_missing_name_returns_empty(self, tmp_path):
        self._tree(tmp_path)
        assert paths.find_model_files(str(tmp_path), "nope") == []


class TestListDeployedModels:
    def test_lists_across_trees_and_skips_examples(self, tmp_path):
        (tmp_path / "workloads/models/inference").mkdir(parents=True)
        (tmp_path / "workloads/scale-models").mkdir(parents=True)
        (tmp_path / "workloads/models/inference/a.yaml").write_text("{}")
        (tmp_path / "workloads/scale-models/b.yaml").write_text("{}")
        (tmp_path / "workloads/scale-models/template.yaml.example").write_text("{}")
        got = paths.list_deployed_models(str(tmp_path))
        names = [n for n, _ in got]
        assert names == ["a", "b"]  # sorted, template.example excluded
