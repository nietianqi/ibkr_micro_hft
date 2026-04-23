from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ibkr_micro_alpha.config import EngineConfig
from ibkr_micro_alpha.execution import ExecutionManager
from ibkr_micro_alpha.types import (
    EngineMode,
    EntryRegime,
    FillEvent,
    IntentAction,
    OrderState,
    OrderStatus,
    PositionSide,
    QuoteUpdate,
    TradeIntent,
    TradeSide,
)


def test_shadow_execution_fills_marketable_order_and_creates_take_profit_follow_up() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    execution = ExecutionManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    quote = QuoteUpdate("AAPL", ts, ts, 100.0, 1000.0, 100.01, 1000.0)

    events = execution._shadow_execute(
        TradeIntent(
            IntentAction.OPEN_LONG,
            "AAPL",
            TradeSide.BUY,
            10,
            100.02,
            ts,
            "test",
            entry_regime=EntryRegime.CONFIRMED_TAKER,
            metadata={"tp_ticks": 1.0},
        ),
        quote,
    )
    fill = next(event for event in events if isinstance(event, FillEvent))
    follow_ups = execution.apply_fill(fill)

    assert execution.position_for("AAPL") is not None
    assert execution.position_for("AAPL").side == PositionSide.LONG
    assert len(follow_ups) == 1
    assert follow_ups[0].action == IntentAction.PLACE_TAKE_PROFIT


def test_protective_exit_preempts_take_profit() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    execution = ExecutionManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    quote = QuoteUpdate("AAPL", ts, ts, 100.0, 1000.0, 100.01, 1000.0)

    entry_events = execution._shadow_execute(
        TradeIntent(
            IntentAction.OPEN_LONG,
            "AAPL",
            TradeSide.BUY,
            10,
            100.02,
            ts,
            "entry",
            entry_regime=EntryRegime.CONFIRMED_TAKER,
            metadata={"tp_ticks": 1.0},
        ),
        quote,
    )
    entry_fill = next(event for event in entry_events if isinstance(event, FillEvent))
    tp_intent = execution.apply_fill(entry_fill)[0]
    execution._shadow_execute(tp_intent, quote)

    generated = asyncio.run(
        execution.execute(
            TradeIntent(
                IntentAction.EXIT,
                "AAPL",
                TradeSide.SELL,
                10,
                100.0,
                ts,
                "protective_exit",
                entry_regime=EntryRegime.CONFIRMED_TAKER,
                reduce_only=True,
            ),
            quote,
        )
    )

    canceled = [event for event in generated if getattr(event, "cancel_reason", "") == "protective_exit_preempts_tp"]
    assert canceled
    assert execution.position_for("AAPL") is not None


def test_partial_fill_reprices_take_profit() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    execution = ExecutionManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    entry_intent = TradeIntent(
        IntentAction.OPEN_LONG,
        "AAPL",
        TradeSide.BUY,
        10,
        100.02,
        ts,
        "entry",
        entry_regime=EntryRegime.CONFIRMED_TAKER,
        metadata={"tp_ticks": 1.0},
    )
    order = execution._create_order_state(entry_intent, OrderStatus.SUBMITTED, ts)
    execution.orders[order.local_order_id] = order

    first_follow_ups = execution.apply_fill(
        FillEvent(order.local_order_id, "AAPL", TradeSide.BUY, 100.01, 4, ts, ts)
    )
    execution._shadow_execute(first_follow_ups[0], None)

    second_follow_ups = execution.apply_fill(
        FillEvent(order.local_order_id, "AAPL", TradeSide.BUY, 100.01, 6, ts + timedelta(milliseconds=10), ts + timedelta(milliseconds=10))
    )

    assert len(second_follow_ups) == 2
    assert second_follow_ups[0].action == IntentAction.CANCEL
    assert second_follow_ups[1].action == IntentAction.PLACE_TAKE_PROFIT


def test_passive_entry_ttl_cancel_marks_retry() -> None:
    config = EngineConfig(mode=EngineMode.SHADOW)
    execution = ExecutionManager(config)
    ts = datetime.now(UTC).replace(tzinfo=None)
    passive_order = execution._create_order_state(
        TradeIntent(
            IntentAction.OPEN_LONG,
            "AAPL",
            TradeSide.BUY,
            10,
            100.0,
            ts,
            "passive",
            entry_regime=EntryRegime.PASSIVE_IMPROVEMENT,
            ttl_ms=250,
        ),
        OrderStatus.SIMULATED,
        ts,
    )
    execution.orders[passive_order.local_order_id] = passive_order

    updates = execution.cancel_expired_entries(ts + timedelta(milliseconds=300))

    assert len(updates) == 1
    assert updates[0].status == OrderStatus.CANCELED
    assert execution.passive_retry_available("AAPL") is False
