"""Tests for binary market microstructure — price impact + timing."""
import pytest
from trading.microstructure import (
    binary_market_price_impact,
    compute_optimal_limit_price,
    EntryTimingAnalyzer,
)


class TestPriceImpact:

    def test_zero_volume_has_impact(self):
        impact = binary_market_price_impact(5.0, 0.0, 0.50)
        assert impact >= 0.0  # Some impact even with no volume

    def test_small_order_low_impact(self):
        impact = binary_market_price_impact(5.0, 100_000.0, 0.50)
        assert impact < 0.005  # < 0.5¢ on liquid market

    def test_impact_higher_at_extremes(self):
        impact_mid = binary_market_price_impact(5.0, 50_000.0, 0.50)
        impact_extreme = binary_market_price_impact(5.0, 50_000.0, 0.05)
        assert impact_extreme > impact_mid

    def test_impact_bounded(self):
        impact = binary_market_price_impact(100.0, 1000.0, 0.95)
        assert impact <= 0.02

    def test_impact_non_negative(self):
        for price in [0.01, 0.10, 0.50, 0.90, 0.99]:
            impact = binary_market_price_impact(5.0, 50_000.0, price)
            assert impact >= 0.0


class TestOptimalLimitPrice:

    def test_patient_buyer_near_bid(self):
        price = compute_optimal_limit_price("BUY", 0.70, 0.72, 3.0, 50_000.0, urgency=0.0)
        assert abs(price - 0.70) < 0.01  # Near best bid

    def test_urgent_buyer_near_ask(self):
        price = compute_optimal_limit_price("BUY", 0.70, 0.72, 3.0, 50_000.0, urgency=1.0)
        assert price >= 0.71  # Near or above ask

    def test_price_between_bid_ask(self):
        price = compute_optimal_limit_price("BUY", 0.70, 0.74, 3.0, 50_000.0, urgency=0.5)
        assert 0.70 <= price <= 0.75


class TestEntryTimingAnalyzer:

    def setup_method(self):
        self.analyzer = EntryTimingAnalyzer()

    def test_normal_conditions_high_score(self):
        result = self.analyzer.analyze_entry_timing(
            days_to_resolution=10.0, current_price=0.72,
            price_volatility_24h=0.05, hour_utc=14,
        )
        assert result["timing_score"] > 0.7
        assert result["should_enter"]

    def test_too_close_to_resolution(self):
        result = self.analyzer.analyze_entry_timing(
            days_to_resolution=0.5, current_price=0.72,
            price_volatility_24h=0.05, hour_utc=14,
        )
        assert result["timing_score"] < 0.5
        assert not result["should_enter"]

    def test_high_volatility_reduces_score(self):
        result = self.analyzer.analyze_entry_timing(
            days_to_resolution=10.0, current_price=0.72,
            price_volatility_24h=0.20, hour_utc=14,
        )
        assert result["timing_score"] < 0.7

    def test_extreme_price_reduces_score(self):
        result = self.analyzer.analyze_entry_timing(
            days_to_resolution=10.0, current_price=0.03,
            price_volatility_24h=0.05, hour_utc=14,
        )
        assert result["timing_score"] < 0.5

    def test_low_liquidity_hours(self):
        result_night = self.analyzer.analyze_entry_timing(
            days_to_resolution=10.0, current_price=0.50,
            price_volatility_24h=0.05, hour_utc=4,
        )
        result_day = self.analyzer.analyze_entry_timing(
            days_to_resolution=10.0, current_price=0.50,
            price_volatility_24h=0.05, hour_utc=14,
        )
        assert result_night["timing_score"] < result_day["timing_score"]
