from __future__ import annotations

from datetime import UTC, datetime

from ibkr_micro_alpha.config import EngineConfig
from ibkr_micro_alpha.risk import HardRiskManager
from ibkr_micro_alpha.types import (
    DecisionContext,
    EngineMode,
    EntryRegime,
    IntentAction,
    MarketDataCapabilities,
    QuoteUpdate,
    SessionHealth,
    SignalFilterState,
    SignalSnapshot,
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
        filters=SignalFilterState(True, 0.0, True, True, 0.0, 5.0, 1.0, False, True, False, ()),
        long_score=3.0,
        short_score=-3.0,
        depth_available=False,
        shortable_tier=3.0,
        shortable_shares=1000,
        entry_regime_candidate=EntryRegime.CONFIRMED_TAKER,
    )


def _context(ts: datetime, *, connected: bool = True, data_stale: bool = False, shortable_tier: float | None = 3.0, shortable_shares: int | None = 1000) -> DecisionContext:
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
