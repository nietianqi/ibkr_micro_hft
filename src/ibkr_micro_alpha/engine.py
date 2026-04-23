from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from uuid import uuid4

from .adapter.base import AbstractBrokerAdapter
from .adapter.ibkr import IBKRAdapter
from .config import EngineConfig
from .event_bus import AsyncEventBus
from .execution import WORKING_ORDER_STATUSES, ExecutionManager
from .logging_utils import configure_logging
from .reporting import render_report
from .risk import HardRiskManager
from .session import classify_session, is_extended_hours
from .signals import SignalCalculator
from .state import MarketStateStore
from .storage import NoopAuditWriter, ParquetAuditWriter, SQLiteStore
from .strategy import DecisionEngine
from .types import (
    BookUpdate,
    BrokerSnapshot,
    DecisionContext,
    EngineEvent,
    EngineMode,
    ExecutionState,
    FillEvent,
    IntentAction,
    MarketMetaUpdate,
    OrderUpdate,
    PositionSide,
    QueueState,
    QuoteUpdate,
    SessionHealth,
    SignalSnapshot,
    StatusUpdate,
    TradeIntent,
    TradePrint,
    TradeSide,
)


@dataclass
class TradingEngine:
    config: EngineConfig
    adapter: AbstractBrokerAdapter | None = None
    enable_audit: bool = True
    logger: logging.Logger = field(init=False)

    def __post_init__(self) -> None:
        configure_logging(self.config)
        self.logger = logging.getLogger("ibkr_micro_alpha.engine")
        self.session_id = str(uuid4())
        self.started_at = datetime.now(UTC).replace(tzinfo=None)
        self.market_states = MarketStateStore()
        self.bus = AsyncEventBus()
        self.adapter = self.adapter or IBKRAdapter(self.config)
        self.signal_calculator = SignalCalculator(self.config, self.market_states)
        self.decision_engine = DecisionEngine(self.config)
        self.risk_manager = HardRiskManager(self.config)
        self.execution = ExecutionManager(self.config, self.adapter)
        self.sqlite_store = SQLiteStore(self.config)
        self.sqlite_store.initialize()
        self.parquet_writer = ParquetAuditWriter(self.config) if self.enable_audit else NoopAuditWriter()
        self.health = SessionHealth(
            connected=False,
            data_stale=False,
            last_event_at=None,
            reconnect_count=0,
            kill_switch_engaged=False,
            pending_orders=0,
            mode=self.config.mode,
            warnings=(),
        )
        self._stop_event = asyncio.Event()
        self._event_count = 0
        self._signal_count = 0
        self._intent_count = 0
        self._last_reconcile_at: datetime | None = None
        self._kill_switch_actions_started = False
        self.bus.subscribe(
            (BookUpdate, QuoteUpdate, TradePrint, StatusUpdate, MarketMetaUpdate, OrderUpdate, FillEvent),
            self._audit_event,
        )
        self.bus.subscribe(SignalSnapshot, self._audit_signal)
        self.bus.subscribe(TradeIntent, self._audit_decision)

    async def run(self) -> None:
        self.adapter.set_publisher(self.bus.publish)
        await self.adapter.connect()
        self.health.connected = True
        await self.adapter.subscribe_market_data(self.config.default_symbols, self.config.ibkr.depth_levels)
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self.config.runtime.heartbeat_seconds)
                if self.config.mode == EngineMode.LIVE:
                    await self._maybe_reconcile()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.parquet_writer.close()
        ended_at = datetime.now(UTC).replace(tzinfo=None)
        self.sqlite_store.record_session_summary(
            session_id=self.session_id,
            started_at=self.started_at,
            ended_at=ended_at,
            health=self.health,
            metrics={
                "events": self._event_count,
                "signals": self._signal_count,
                "intents": self._intent_count,
            },
        )
        self.sqlite_store.close()
        if self.adapter is not None:
            await self.adapter.disconnect()

    async def stop(self) -> None:
        self._stop_event.set()

    async def _audit_event(self, event: EngineEvent) -> None:
        self._event_count += 1
        event_ts_local = getattr(event, "ts_local", datetime.now(UTC).replace(tzinfo=None))
        self.health.last_event_at = event_ts_local
        self.health.pending_orders = sum(self.execution.pending_orders(symbol) for symbol in self.config.default_symbols)
        self.health.kill_switch_engaged = self.risk_manager.kill_switch_engaged
        self.health.kill_switch_reason = self.risk_manager.kill_switch_reason
        self.risk_manager.update_open_positions(self.execution.open_positions_count())
        self.parquet_writer.write_event(event)
        await self._handle_event(event)

    async def _audit_signal(self, signal: SignalSnapshot) -> None:
        self._signal_count += 1
        self.parquet_writer.write_signal(signal)

    async def _audit_decision(self, decision: TradeIntent) -> None:
        self._intent_count += 1
        self.parquet_writer.write_decision(decision)

    async def _handle_event(self, event: EngineEvent) -> None:
        if hasattr(event, "ts_local"):
            for generated in self.execution.cancel_expired_entries(event.ts_local):
                await self.bus.publish(generated)

        if isinstance(event, QuoteUpdate):
            self.execution.update_unrealized(event)
            position = self.execution.positions.get(event.symbol)
            if position is not None:
                self.sqlite_store.record_position(position)
            for generated in self.execution.simulate_passive_orders(event):
                await self.bus.publish(generated)

        if isinstance(event, OrderUpdate):
            self.execution.apply_order_update(event)
            order = self.execution.orders.get(event.local_order_id)
            if order is not None:
                self.sqlite_store.record_order(order)
            return

        if isinstance(event, FillEvent):
            self.sqlite_store.record_fill(event)
            follow_ups = self.execution.apply_fill(event)
            position = self.execution.positions.get(event.symbol)
            if position is not None:
                self.sqlite_store.record_position(position)
                if position.quantity == 0:
                    self.risk_manager.register_closed_position(position)
            for follow_up in follow_ups:
                await self._process_intent(follow_up)
            return

        if isinstance(event, StatusUpdate):
            self.health.connected = event.status not in {"error", "connection_closed"}
            if event.status == "connection_closed":
                self.health.reconnect_count += 1
            return

        if isinstance(event, (BookUpdate, QuoteUpdate, TradePrint, MarketMetaUpdate)):
            signal = self.signal_calculator.on_event(event)
            if self.config.mode == EngineMode.CAPTURE and not self.config.runtime.capture_signals_in_capture_mode:
                return
            if signal is None:
                return
            await self.bus.publish(signal)
            context = self._build_decision_context(signal.symbol, signal, event.ts_event)
            intent = self.decision_engine.decide(context)
            if intent is not None:
                await self._process_intent(intent)

    def _build_decision_context(self, symbol: str, signal: SignalSnapshot | None, ts_event: datetime) -> DecisionContext:
        state = self.market_states.state_for(symbol)
        warnings: list[str] = []
        quote = state.quote
        signal_time = signal.ts_local if signal is not None else datetime.now(UTC).replace(tzinfo=None)
        session_regime = signal.session_regime if signal is not None else classify_session(self.config, ts_event)
        if quote is not None:
            age_ms = (signal_time - quote.ts_event).total_seconds() * 1000.0
            self.health.data_stale = age_ms > self.config.risk.stale_quote_kill_ms
            if self.health.data_stale:
                warnings.append("quote_stale")
        self.health.warnings = tuple(warnings)
        max_order_qty = self.config.symbol_config(symbol).max_shares
        short_inventory_ok = self.risk_manager.short_inventory_ok(max_order_qty, state.shortable_tier, state.shortable_shares)
        return DecisionContext(
            symbol=symbol,
            ts_event=ts_event,
            mode=self.config.mode,
            signal=signal,
            quote=quote,
            position=self.execution.position_for(symbol),
            session_health=self.health,
            pending_orders=self.execution.pending_orders(symbol),
            session_regime=session_regime,
            extended_hours=is_extended_hours(session_regime),
            queue_state=signal.queue_state if signal is not None else QueueState.NORMAL,
            execution_state=signal.execution_state if signal is not None else ExecutionState.NORMAL,
            depth_available=state.capabilities().depth_available,
            short_inventory_ok=short_inventory_ok,
            shortable_tier=state.shortable_tier,
            shortable_shares=state.shortable_shares,
            market_data_capabilities=state.capabilities(),
            passive_retry_available=self.execution.passive_retry_available(symbol),
        )

    async def _process_intent(self, intent: TradeIntent) -> None:
        quote = self.market_states.state_for(intent.symbol).quote
        context = self._build_decision_context(
            intent.symbol,
            self.signal_calculator.latest_signals.get(intent.symbol),
            intent.ts_event,
        )
        self.risk_manager.update_open_positions(self.execution.open_positions_count())
        risk = self.risk_manager.evaluate(context, intent)
        self.sqlite_store.record_risk(intent.symbol, risk)
        self.parquet_writer.write_decision(risk)
        if not risk.allowed:
            self.health.kill_switch_engaged = risk.kill_switch
            self.health.kill_switch_reason = risk.reason if risk.kill_switch else self.health.kill_switch_reason
            if risk.kill_switch:
                await self._engage_kill_switch(risk.reason, intent.ts_event)
            return

        adjusted_intent = intent
        if risk.max_quantity != intent.quantity:
            adjusted_intent = TradeIntent(
                action=intent.action,
                symbol=intent.symbol,
                side=intent.side,
                quantity=risk.max_quantity,
                limit_price=intent.limit_price,
                ts_event=intent.ts_event,
                reason=intent.reason,
                take_profit_price=intent.take_profit_price,
                entry_regime=intent.entry_regime,
                execution_state=intent.execution_state,
                max_slippage_ticks=intent.max_slippage_ticks,
                reduce_only=intent.reduce_only,
                ttl_ms=intent.ttl_ms,
                purpose_detail=intent.purpose_detail,
                metadata=dict(intent.metadata),
            )
        await self.bus.publish(adjusted_intent)
        for event in await self.execution.execute(adjusted_intent, quote):
            await self.bus.publish(event)

    async def _engage_kill_switch(self, reason: str, ts_event: datetime) -> None:
        if self._kill_switch_actions_started:
            return
        self._kill_switch_actions_started = True
        self.health.kill_switch_engaged = True
        self.health.kill_switch_reason = reason
        working_orders = [
            order
            for order in self.execution.orders.values()
            if order.status in WORKING_ORDER_STATUSES
        ]
        for order in list(working_orders):
            if order.reduce_only:
                continue
            await self._process_intent(
                TradeIntent(
                    action=IntentAction.CANCEL,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=0,
                    limit_price=None,
                    ts_event=ts_event,
                    reason="kill_switch_cancel",
                    reduce_only=True,
                    purpose_detail="kill_switch_cancel",
                    metadata={"target_local_order_id": order.local_order_id},
                )
            )
        for position in list(self.execution.positions.values()):
            if position.quantity <= 0 or position.side == PositionSide.FLAT:
                continue
            quote = self.market_states.state_for(position.symbol).quote
            if quote is None:
                continue
            if position.side == PositionSide.LONG:
                side = TradeSide.SELL
                limit_price = quote.bid_price
            else:
                side = TradeSide.BUY
                limit_price = quote.ask_price
            await self._process_intent(
                TradeIntent(
                    action=IntentAction.FLATTEN,
                    symbol=position.symbol,
                    side=side,
                    quantity=position.quantity,
                    limit_price=limit_price,
                    ts_event=ts_event,
                    reason="kill_switch_flatten",
                    entry_regime=position.entry_regime,
                    reduce_only=True,
                    purpose_detail="kill_switch_flatten",
                )
            )
        if self.config.mode == EngineMode.LIVE and self.adapter is not None:
            try:
                await self.flatten()
            except Exception:  # pragma: no cover - best effort during live emergency handling
                self.logger.exception("kill switch flatten fallback failed")

    async def _maybe_reconcile(self) -> None:
        if self.adapter is None:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        if self._last_reconcile_at is not None:
            elapsed = (now - self._last_reconcile_at).total_seconds()
            if elapsed < self.config.runtime.reconcile_interval_seconds:
                return
        snapshot = await self.adapter.request_reconcile()
        self._last_reconcile_at = now
        details = self._reconcile_details(snapshot)
        if details is None:
            return
        risk = self.risk_manager.register_reconcile_mismatch(details)
        self.sqlite_store.record_risk(None, risk)
        self.parquet_writer.write_decision(risk)
        await self._engage_kill_switch(risk.reason, now)

    def _reconcile_details(self, snapshot: BrokerSnapshot) -> str | None:
        local_positions = {
            symbol: (position.side.value, position.quantity)
            for symbol, position in self.execution.positions.items()
            if position.quantity > 0 and position.side != PositionSide.FLAT
        }
        broker_positions = {
            position.symbol: (position.side.value, position.quantity)
            for position in snapshot.positions
            if position.quantity > 0 and position.side != PositionSide.FLAT
        }
        if local_positions != broker_positions:
            return f"positions local={local_positions} broker={broker_positions}"
        return None

    async def flatten(self) -> BrokerSnapshot | None:
        if self.adapter is None:
            return None
        snapshot = await self.adapter.request_reconcile()
        for order in snapshot.open_orders:
            if order.broker_order_id is not None:
                await self.adapter.cancel_order(order.broker_order_id)
        flatten_symbols = [position.symbol for position in snapshot.positions if position.quantity > 0 and position.side != PositionSide.FLAT]
        if flatten_symbols:
            await self.adapter.subscribe_market_data(flatten_symbols, depth_levels=1)
            await asyncio.sleep(1.0)
        for position in snapshot.positions:
            if position.quantity <= 0 or position.side == PositionSide.FLAT:
                continue
            quote = self.market_states.state_for(position.symbol).quote
            if quote is None:
                continue
            if position.side == PositionSide.LONG:
                side = TradeSide.SELL
                limit_price = quote.bid_price
            else:
                side = TradeSide.BUY
                limit_price = quote.ask_price
            intent = TradeIntent(
                action=IntentAction.FLATTEN,
                symbol=position.symbol,
                side=side,
                quantity=position.quantity,
                limit_price=limit_price,
                ts_event=datetime.now(UTC).replace(tzinfo=None),
                reason="manual_flatten",
                entry_regime=position.entry_regime,
                reduce_only=True,
                purpose_detail="manual_flatten",
            )
            await self._process_intent(intent)
        return snapshot

    async def reconcile(self) -> BrokerSnapshot | None:
        if self.adapter is None:
            return None
        return await self.adapter.request_reconcile()


async def run_engine(config: EngineConfig) -> None:
    engine = TradingEngine(config)
    await engine.run()


async def flatten_book(config: EngineConfig) -> str:
    engine = TradingEngine(config, enable_audit=False)
    if engine.adapter is None:
        return "capture mode has no broker adapter"
    engine.adapter.set_publisher(engine.bus.publish)
    await engine.adapter.connect()
    snapshot = await engine.flatten()
    await engine.shutdown()
    if snapshot is None:
        return "no broker snapshot available"
    return f"Canceled {len(snapshot.open_orders)} orders; positions seen={len(snapshot.positions)}."


async def reconcile_book(config: EngineConfig) -> str:
    engine = TradingEngine(config, enable_audit=False)
    if engine.adapter is None:
        return "capture mode has no broker adapter"
    engine.adapter.set_publisher(engine.bus.publish)
    await engine.adapter.connect()
    snapshot = await engine.reconcile()
    await engine.shutdown()
    if snapshot is None:
        return "no broker snapshot available"
    return (
        f"Broker snapshot at {snapshot.ts_local.isoformat()} "
        f"positions={len(snapshot.positions)} open_orders={len(snapshot.open_orders)}"
    )


def build_report(config: EngineConfig, trading_date: str | None = None) -> str:
    return render_report(config, trading_date=trading_date)
