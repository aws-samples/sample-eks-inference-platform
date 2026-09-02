"""Tests for the dashboard's tokens-per-dollar efficiency metric.

`_tok_per_dollar` turns the two data sources the dashboard already has (LiteLLM
throughput + the cost engine) into one comparable efficiency number. Its edge
cases carry meaning the Cost view relies on: None = no cost basis (hide), 0 =
idle-but-billing (flag as waste), so they're behavioural contracts, not cosmetics.
"""

import backend


class TestTokPerDollar:
    def test_normal_ratio_is_tokens_per_hour_over_dollars_per_hour(self):
        # 100 tok/s = 360,000 tok/hr; at $2/hr that's 180,000 tok/$.
        assert backend._tok_per_dollar(100, 2.0) == 180000
        assert backend._tok_per_dollar(50, 1.5) == 120000

    def test_idle_but_billing_is_zero_not_none(self):
        # Model costs money but produced no tokens in the window -> 0 (waste),
        # which the Cost view surfaces distinctly from "no cost basis".
        assert backend._tok_per_dollar(0, 2.0) == 0
        assert backend._tok_per_dollar(None, 2.0) == 0

    def test_no_cost_basis_is_none(self):
        # No $/hr basis (idle/unpriced) -> ratio undefined -> None (hidden).
        assert backend._tok_per_dollar(100, 0) is None
        assert backend._tok_per_dollar(100, None) is None
        assert backend._tok_per_dollar(100, -1) is None

    def test_bad_input_is_none_not_an_exception(self):
        assert backend._tok_per_dollar("x", 2.0) is None
        assert backend._tok_per_dollar(100, "nan-ish") is None

    def test_rounds_to_whole_tokens(self):
        assert isinstance(backend._tok_per_dollar(1, 3.0), int)
