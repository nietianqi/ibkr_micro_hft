from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ibkr_micro_alpha.config import EngineConfig
from ibkr_micro_alpha.strategy import DecisionEngine
from ibkr_micro_alpha.types import (
    DecisionContext,
    EngineMode,
    EntryRegime,
    MarketDataCapabilities,
    PositionState,
    PositionSide,
    QuoteUpdate,
    SessionHealth,
    SignalFilterState,
    SignalSnapshot,
)


def _signal_snapshot(
    ts: datetime,
    *,
    long_score: float,
    short_score: float,
    spread_ticks: float,
    depth_available: bool,
    weighted_imbalance: float = 0.4,
    lob_ofi: float = 3.0,
) -> SignalSnapshot:
    return SignalSnapshot(
        symbol="AAPL",
        ts_event=ts,
        ts_local=ts,
        weighted_imbalance=weighted_imbalance,
        lob_ofi=lob_ofi,
        l1_imbalance=0.4,
        quote_ofi=5.0,
        tape_ofi=5.0,
        trade_burst=5.0,
        microprice=100.005,
        microprice_momentum=1.0,
        microprice_tilt=0.2,
        zscores={
            "quote_ofi": 2.0,
            "tape_ofi": 2.0,
            "l1_imbalance": 2.0,
            "trade_burst": 2.0,
            "microprice_tilt": 2.0,
            "microprice_momentum": 2.0,
            "weighted_imbalance": 1.0,
            "lob_ofi": 1.0,
        },
        filters=SignalFilterState(
            market_ok=True,
            linkage_score=0.0,
            overheat_long_ok=True,
            overheat_short_ok=True,
            quote_age_ms=0.0,
            trade_rate_per_sec=10.0,
            spread_ticks=spread_ticks,
            depth_available=depth_available,
            short_inventory_ok=True,
            abnormal=False,
            reasons=(),
        ),
        long_score=long_score,
        short_score=short_score,
        depth_available=depth_available,
        shortable_tier=3.0,
        shortable_shares=1000,
        entry_regime_candidate=EntryRegime.NONE,
    )


def _context(signal: SignalSnapshot, quote: QuoteUpdate) -> DecisionContext:
    return DecisionContext(
        symbol="AAPL",
        ts_event=signal.ts_event,
        mode=EngineMode.SHADOW,
        signal=signal,
        quote=quote,
        position=None,
        session_health=SessionHealth(True, False, signal.ts_event, 0, False, 0, EngineMode.SHADOW),
        pending_orders=0,
        depth_available=signal.depth_available,
        short_inventory_ok=True,
        shortable_tier=signal.shortable_tier,
        shortable_shares=signal.shortable_shares,
        market_data_capabilities=MarketDataCapabilities(
            tick_by_tick_bidask=True,
            tick_by_tick_trades=True,
            depth_available=signal.depth_available,
            shortable_data_available=True,
            generic_ticks_available=True,
        ),
        passive_retry_available=True,
    )


def test_decision_engine_prefers_confirmed_taker_entry() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    decision_engine = DecisionEngine(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    signal = _signal_snapshot(ts, long_score=3.0, short_score=-3.0, spread_ticks=1.0, depth_available=False)
    quote = QuoteUpdate("AAPL", ts, ts, 100.00, 200.0, 100.01, 100.0)

    intent = decision_engine.decide(_context(signal, quote))

    assert intent is not None
    assert intent.entry_regime == EntryRegime.CONFIRMED_TAKER
    assert intent.limit_price == pytest.approx(100.02)


def test_decision_engine_uses_passive_improvement_when_depth_bonus_is_available() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    decision_engine = DecisionEngine(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    signal = _signal_snapshot(ts, long_score=2.0, short_score=-2.0, spread_ticks=2.0, depth_available=True)
    quote = QuoteUpdate("AAPL", ts, ts, 100.00, 200.0, 100.02, 100.0)

    intent = decision_engine.decide(_context(signal, quote))

    assert intent is not None
    assert intent.entry_regime == EntryRegime.PASSIVE_IMPROVEMENT
    assert intent.limit_price == quote.bid_price
    assert intent.ttl_ms == config.strategy.entry_regime_defaults.passive_entry_ttl_ms


def test_decision_engine_triggers_protective_exit_for_open_position() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    decision_engine = DecisionEngine(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    signal = _signal_snapshot(ts, long_score=0.2, short_score=-0.2, spread_ticks=1.0, depth_available=False)
    quote = QuoteUpdate("AAPL", ts, ts, 100.00, 200.0, 100.01, 100.0)
    position = PositionState(
        symbol="AAPL",
        side=PositionSide.LONG,
        quantity=10,
        avg_price=99.90,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        opened_at=ts,
        updated_at=ts,
        entry_regime=EntryRegime.CONFIRMED_TAKER,
    )
    context = _context(signal, quote)
    context.position = position

    intent = decision_engine.decide(context)

    assert intent is not None
    assert intent.action.value == "exit"
    assert intent.reduce_only is True
