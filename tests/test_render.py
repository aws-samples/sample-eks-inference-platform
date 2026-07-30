"""Tests for model-name derivation and the manifest-emission injection guard."""

import pytest
import yaml

from recommend_instance import render


class TestModelNameFromId:
    def test_kebab_slug(self):
        assert render.model_name_from_id("meta-llama/Llama-3.1-8B") == "llama-3-1-8b"
        assert render.model_name_from_id("Qwen/Qwen3-32B") == "qwen3-32b"
        assert render.model_name_from_id("org/a_b.c") == "a-b-c"

    def test_derived_name_is_a_valid_k8s_label(self):
        from recommend_instance import paths
        for mid in ("meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen3-32B", "gpt2"):
            assert paths.is_valid_model_name(render.model_name_from_id(mid)), mid


class _StubModel:
    """Minimal stand-in for ModelSpec — build_endpoint_yaml validates model_id
    before it touches any other field, so only model_id is needed."""

    def __init__(self, model_id):
        self.model_id = model_id


class TestBuildEndpointYamlGuard:
    @pytest.mark.parametrize("bad_id", [
        'org/model"\ninjected: true',
        "org/model\nname: evil",
        'x"; rm -rf /',
        "a b/c",
        "org/a/b",
        "../../etc/passwd",
    ])
    def test_rejects_unsafe_model_id_before_emitting(self, bad_id):
        # The guard must sys.exit (SystemExit) rather than emit a manifest with an
        # attacker-controlled model id. best/vram/args are unused before the guard.
        with pytest.raises(SystemExit):
            render.build_endpoint_yaml("VLLMEndpoint", _StubModel(bad_id), None, None, None)


class TestEmittedManifestShape:
    def test_valid_id_round_trips_as_yaml(self):
        # Mirror the exact interpolation build_endpoint_yaml uses for the two
        # externally-derived fields, and prove a validated id round-trips cleanly.
        mid = "meta-llama/Llama-3.1-8B-Instruct"
        name = render.model_name_from_id(mid)
        body = (
            "apiVersion: kro.run/v1alpha1\n"
            "kind: VLLMEndpoint\n"
            "metadata:\n"
            f"  name: {name}\n"
            "  namespace: inference\n"
            "spec:\n"
            f'  model: "{mid}"\n'
        )
        doc = list(yaml.safe_load_all(body))[0]
        assert doc["spec"]["model"] == mid
        assert doc["metadata"]["name"] == "llama-3-1-8b-instruct"
