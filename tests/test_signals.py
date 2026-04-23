from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ibkr_micro_alpha.config import EngineConfig, SymbolConfig
from ibkr_micro_alpha.signals import SignalCalculator
from ibkr_micro_alpha.state import MarketStateStore
from ibkr_micro_alpha.types import EngineMode, ExecutionState, QueueState, QuoteUpdate


def test_signal_calculator_uses_bidask_for_l1_metrics_without_depth() -> None:
    config = EngineConfig()
    calculator = SignalCalculator(config=config, market_states=MarketStateStore())
    base_ts = datetime(2026, 4, 22, 14, 30, tzinfo=UTC).replace(tzinfo=None)

    for idx in range(6):
        snapshot = calculator.on_event(
            QuoteUpdate(
                symbol="AAPL",
                ts_event=base_ts + timedelta(milliseconds=idx * 100),
                ts_local=base_ts + timedelta(milliseconds=idx * 100),
                bid_price=100.00 + (idx * 0.01),
                bid_size=220.0 + (idx * 10),
                ask_price=100.02 + (idx * 0.01),
                ask_size=200.0,
                source="ibkr_tick_by_tick_bidask",
            )
        )

    assert snapshot is not None
    assert snapshot.depth_available is False
    assert snapshot.l1_imbalance > 0
    assert snapshot.quote_ofi > 0
    assert snapshot.microprice > snapshot.metadata["mid_price"]
    assert snapshot.entry_regime_candidate.value in {"none", "confirmed_taker"}
    assert snapshot.session_regime.value == "core"
    assert snapshot.session_trade_allowed is True
    assert snapshot.reservation_bias > 0
    assert snapshot.queue_state == QueueState.NORMAL
    assert snapshot.execution_state == ExecutionState.NORMAL


def test_signal_calculator_blocks_live_extended_hours_for_tier_b_symbol() -> None:
    config = EngineConfig(mode=EngineMode.LIVE)
    calculator = SignalCalculator(config=config, market_states=MarketStateStore())
    base_ts = datetime(2026, 4, 22, 12, 10, tzinfo=UTC).replace(tzinfo=None)

    for idx in range(6):
        snapshot = calculator.on_event(
            QuoteUpdate(
                symbol="AMD",
                ts_event=base_ts + timedelta(milliseconds=idx * 100),
                ts_local=base_ts + timedelta(milliseconds=idx * 100),
                bid_price=100.00 + (idx * 0.01),
                bid_size=300.0,
                ask_price=100.01 + (idx * 0.01),
                ask_size=300.0,
                source="ibkr_tick_by_tick_bidask",
            )
        )

    assert snapshot is not None
    assert snapshot.session_regime.value == "pre"
    assert snapshot.session_trade_allowed is False
    assert "extended_hours_live_disabled" in snapshot.filters.session_reasons


def test_signal_calculator_marks_one_tick_spread_as_queue_state() -> None:
    config = EngineConfig()
    config.symbols["AAPL"] = SymbolConfig(
        tick_size=0.01,
        max_shares=50,
        min_top_depth=100.0,
        min_trade_rate=0.0,
        max_spread_ticks=2.0,
    )
    calculator = SignalCalculator(config=config, market_states=MarketStateStore())
    base_ts = datetime(2026, 4, 22, 14, 30, tzinfo=UTC).replace(tzinfo=None)

    for idx in range(6):
        snapshot = calculator.on_event(
            QuoteUpdate(
                symbol="AAPL",
                ts_event=base_ts + timedelta(milliseconds=idx * 100),
                ts_local=base_ts + timedelta(milliseconds=idx * 100),
                bid_price=100.00 + (idx * 0.01),
                bid_size=300.0,
                ask_price=100.01 + (idx * 0.01),
                ask_size=300.0,
                source="ibkr_tick_by_tick_bidask",
            )
        )

        assert snapshot is not None
    assert snapshot.filters.spread_ticks == pytest.approx(1.0)
    assert snapshot.queue_state == QueueState.THIN
    assert snapshot.execution_state == ExecutionState.QUEUE
