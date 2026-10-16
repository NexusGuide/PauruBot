"""Reseller ranking score behavior."""

from paurubot.core.reseller_ranking import score


def test_reseller_score_rewards_sales_and_revenue() -> None:
    assert score(10, 1_000_000, 0) == 110
    assert score(10, 1_000_000, 2) == 70


def test_reseller_score_never_goes_below_zero() -> None:
    assert score(0, 0, 1_000) == 0
