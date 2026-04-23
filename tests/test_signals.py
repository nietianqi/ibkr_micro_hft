from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ibkr_micro_alpha.config import EngineConfig
from ibkr_micro_alpha.signals import SignalCalculator
from ibkr_micro_alpha.state import MarketStateStore
from ibkr_micro_alpha.types import QuoteUpdate


def test_signal_calculator_uses_bidask_for_l1_metrics_without_depth() -> None:
    config = EngineConfig()
    calculator = SignalCalculator(config=config, market_states=MarketStateStore())
    base_ts = datetime.now(UTC).replace(tzinfo=None)

    for idx in range(6):
        snapshot = calculator.on_event(
            QuoteUpdate(
                symbol="AAPL",
                ts_event=base_ts + timedelta(milliseconds=idx * 100),
                ts_local=base_ts + timedelta(milliseconds=idx * 100),
                bid_price=100.00 + (idx * 0.01),
                bid_size=200.0 + (idx * 10),
                ask_price=100.01 + (idx * 0.01),
                ask_size=100.0,
                source="ibkr_tick_by_tick_bidask",
            )
        )

    assert snapshot is not None
    assert snapshot.depth_available is False
    assert snapshot.l1_imbalance > 0
    assert snapshot.quote_ofi > 0
    assert snapshot.microprice > snapshot.metadata["mid_price"]
    assert snapshot.entry_regime_candidate.value in {"none", "confirmed_taker"}
