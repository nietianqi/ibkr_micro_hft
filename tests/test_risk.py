from __future__ import annotations

from datetime import UTC, datetime

from ibkr_micro_alpha.config import EngineConfig, SymbolConfig
from ibkr_micro_alpha.risk import HardRiskManager
from ibkr_micro_alpha.types import (
    DecisionContext,
    EngineMode,
    EntryRegime,
    ExecutionState,
    IntentAction,
    MarketDataCapabilities,
    QueueState,
    QuoteUpdate,
    SessionRegime,
    SessionHealth,
    SignalFilterState,
    SignalSnapshot,
    SymbolTier,
    TradeIntent,
    TradeSide,
)


def _signal_snapshot(ts: datetime) -> SignalSnapshot:
    return SignalSnapshot(
        symbol="AAPL",
        ts_event=ts,
        ts_local=ts,
        weighted_imbalance=0.5,
        lob_ofi=10.0,
        l1_imbalance=0.5,
        quote_ofi=20.0,
        tape_ofi=20.0,
        trade_burst=10.0,
        microprice=100.005,
        microprice_momentum=0.01,
        microprice_tilt=0.2,
        zscores={
            "quote_ofi": 1.0,
            "tape_ofi": 1.0,
            "l1_imbalance": 1.0,
            "trade_burst": 1.0,
            "microprice_tilt": 1.0,
            "microprice_momentum": 1.0,
            "weighted_imbalance": 1.0,
            "lob_ofi": 1.0,
        },
        filters=SignalFilterState(
            market_ok=True,
            linkage_score=0.0,
            overheat_long_ok=True,
            overheat_short_ok=True,
            quote_age_ms=0.0,
            trade_rate_per_sec=5.0,
            spread_ticks=1.0,
            depth_available=False,
            short_inventory_ok=True,
            queue_state=QueueState.NORMAL,
            abnormal=False,
            reasons=(),
            session_reasons=(),
        ),
        long_score=3.0,
        short_score=-3.0,
        depth_available=False,
        agreement_count_long=6,
        agreement_count_short=0,
        linkage_score=0.0,
        reservation_bias=0.5,
        market_ok=True,
        abnormal=False,
        queue_state=QueueState.NORMAL,
        execution_state=ExecutionState.NORMAL,
        session_regime=SessionRegime.CORE,
        higher_tf_regime_score=0.0,
        session_trade_allowed=True,
        shortable_tier=3.0,
        shortable_shares=1000,
        entry_regime_candidate=EntryRegime.CONFIRMED_TAKER,
    )


def _context(
    ts: datetime,
    *,
    connected: bool = True,
    data_stale: bool = False,
    shortable_tier: float | None = 3.0,
    shortable_shares: int | None = 1000,
    session_regime: SessionRegime = SessionRegime.CORE,
    extended_hours: bool = False,
) -> DecisionContext:
    quote = QuoteUpdate(
        symbol="AAPL",
        ts_event=ts,
        ts_local=ts,
        bid_price=100.0,
        bid_size=500.0,
        ask_price=100.01,
        ask_size=500.0,
    )
    return DecisionContext(
        symbol="AAPL",
        ts_event=ts,
        mode=EngineMode.SHADOW,
        signal=_signal_snapshot(ts),
        quote=quote,
        position=None,
        session_health=SessionHealth(connected, data_stale, ts, 0, False, 0, EngineMode.SHADOW),
        pending_orders=0,
        session_regime=session_regime,
        extended_hours=extended_hours,
        queue_state=QueueState.NORMAL,
        execution_state=ExecutionState.NORMAL,
        depth_available=False,
        short_inventory_ok=True,
        shortable_tier=shortable_tier,
        shortable_shares=shortable_shares,
        market_data_capabilities=MarketDataCapabilities(),
        passive_retry_available=True,
    )


def test_risk_manager_kills_on_stale_data() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    risk = HardRiskManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    intent = TradeIntent(IntentAction.OPEN_LONG, "AAPL", TradeSide.BUY, 10, 100.01, ts, "entry")

    decision = risk.evaluate(_context(ts, connected=False, data_stale=True), intent)

    assert decision.allowed is False
    assert decision.kill_switch is True
    assert decision.reason in {"connection_lost", "data_stale"}


def test_risk_manager_blocks_short_without_inventory() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    risk = HardRiskManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    intent = TradeIntent(IntentAction.OPEN_SHORT, "AAPL", TradeSide.SELL, 50, 100.0, ts, "entry")

    decision = risk.evaluate(_context(ts, shortable_tier=1.0, shortable_shares=10), intent)

    assert decision.allowed is False
    assert decision.reason == "short_inventory_block"


def test_reconcile_mismatch_engages_kill_switch() -> None:
    risk = HardRiskManager(EngineConfig())

    decision = risk.register_reconcile_mismatch("positions local={'AAPL': ('long', 10)} broker={}")

    assert decision.kill_switch is True
    assert decision.reason == "reconcile_mismatch"
    assert risk.kill_switch_engaged is True


def test_risk_manager_clamps_quantity_by_session_scale() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    risk = HardRiskManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    intent = TradeIntent(IntentAction.OPEN_LONG, "AAPL", TradeSide.BUY, 50, 100.01, ts, "entry")

    decision = risk.evaluate(_context(ts, session_regime=SessionRegime.PRE), intent)

    assert decision.allowed is True
    assert decision.reason == "clamped_quantity"
    assert decision.max_quantity == 20


def test_risk_manager_blocks_extended_hours_short_when_symbol_not_enabled() -> None:
    config = EngineConfig(mode=EngineMode.LIVE)
    config.symbols["AAPL"] = SymbolConfig(
        tick_size=0.01,
        max_shares=50,
        tier=SymbolTier.TIER_B,
        allow_extended_hours=True,
        allow_short_extended=False,
    )
    risk = HardRiskManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    intent = TradeIntent(IntentAction.OPEN_SHORT, "AAPL", TradeSide.SELL, 20, 100.00, ts, "entry")

    decision = risk.evaluate(_context(ts, session_regime=SessionRegime.PRE, extended_hours=True), intent)

    assert decision.allowed is False
    assert decision.reason == "extended_hours_short_disabled"


def test_risk_manager_applies_queue_size_scale() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    risk = HardRiskManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    intent = TradeIntent(IntentAction.OPEN_LONG, "AAPL", TradeSide.BUY, 50, 100.01, ts, "entry")
    context = _context(ts, session_regime=SessionRegime.CORE)
    context.execution_state = ExecutionState.QUEUE

    decision = risk.evaluate(context, intent)

    assert decision.allowed is True
    assert decision.reason == "clamped_quantity"
    assert decision.max_quantity == 37
