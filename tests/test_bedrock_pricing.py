"""Tests for the Bedrock pricing unit-scale resolver and id/partition helpers.

The pricing path must never GUESS per-1K vs per-1M from free text: a per-1M
dimension with a bare 'tokens' unit, scaled as per-1K, records a per-token cost
1000x too high into the committed CR. `_tokens_per_unit` returns None on an
ambiguous unit so the caller falls back rather than recording a wrong price.
"""

import pytest

from recommend_instance import bedrock


class TestTokensPerUnit:
    @pytest.mark.parametrize("unit,desc,expected", [
        ("1K tokens", "", 1_000),
        ("1M tokens", "", 1_000_000),
        ("tokens", "Price per 1000 input tokens", 1_000),
        ("tokens", "USD per 1,000,000 output tokens", 1_000_000),
        ("", "per 1M tokens", 1_000_000),
        # Ambiguous — must be None (do NOT default to per-1K).
        ("tokens", "Input tokens", None),
        ("tokens", "", None),
        ("", "", None),
    ])
    def test_scale(self, unit, desc, expected):
        assert bedrock._tokens_per_unit(unit, desc) == expected

    def test_ambiguous_never_defaults_to_1k(self):
        # The exact regression: a bare 'tokens' unit must not be assumed per-1K.
        assert bedrock._tokens_per_unit("tokens", "Input tokens") is None

    def test_clamp_threshold_is_sane(self):
        # $100/1M = 1e-4/token is plausible (below clamp); $1000/1M = 1e-3 is the
        # cutoff for a unit-scale error.
        assert 1e-4 < bedrock._MAX_PLAUSIBLE_PER_TOKEN <= 1e-3


class TestBedrockHelpers:
    def test_default_alias(self):
        assert bedrock.default_alias("amazon.nova-lite-v1:0") == "nova-lite"

    def test_partition_and_endpoint(self):
        assert bedrock.partition_for("eu-central-1") == "aws"
        assert bedrock.partition_for("eusc-de-east-1") == "aws-eusc"
        assert bedrock.runtime_endpoint("eusc-de-east-1", "aws-eusc").endswith("amazonaws.eu")

    def test_region_geo(self):
        assert bedrock._region_geo("us-east-1") == "us"
        assert bedrock._region_geo("eu-central-1") == "eu"
        assert bedrock._region_geo("ap-south-1") == "apac"
