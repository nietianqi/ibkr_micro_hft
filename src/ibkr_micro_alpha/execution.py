from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .adapter.base import AbstractBrokerAdapter
from .config import EngineConfig
from .types import (
    EngineMode,
    EntryRegime,
    FillEvent,
    IntentAction,
    OrderState,
    OrderStatus,
    OrderUpdate,
    PositionSide,
    PositionState,
    QuoteUpdate,
    TradeIntent,
    TradeSide,
)


WORKING_ORDER_STATUSES = {
    OrderStatus.NEW,
    OrderStatus.SUBMITTED,
    OrderStatus.SIMULATED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_CANCEL,
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(slots=True)
class ExecutionManager:
    config: EngineConfig
    adapter: AbstractBrokerAdapter | None = None
    positions: dict[str, PositionState] = field(default_factory=dict)
    orders: dict[str, OrderState] = field(default_factory=dict)
    passive_entry_retries: dict[str, int] = field(default_factory=dict)

    async def execute(self, intent: TradeIntent, quote: QuoteUpdate | None) -> list[OrderUpdate | FillEvent]:
        if intent.action == IntentAction.NOOP:
            return []
        if self.config.mode == EngineMode.CAPTURE:
            return []

        generated: list[OrderUpdate | FillEvent] = []
        if intent.action in {IntentAction.EXIT, IntentAction.FLATTEN}:
            generated.extend(await self._cancel_take_profit_orders(intent.symbol, reason="protective_exit_preempts_tp"))

        if intent.action == IntentAction.CANCEL:
            update = await self._cancel_target_order(intent, reason=intent.reason or "cancel_requested")
            return generated + ([update] if update is not None else [])

        if self.config.mode == EngineMode.SHADOW:
            return generated + self._shadow_execute(intent, quote)
        return generated + await self._live_execute(intent)

    async def _live_execute(self, intent: TradeIntent) -> list[OrderUpdate]:
        if self.adapter is None:
            raise RuntimeError("live mode requires broker adapter")
        order_state = self._create_order_state(intent, OrderStatus.NEW)
        self.orders[order_state.local_order_id] = order_state
        if intent.action == IntentAction.PLACE_TAKE_PROFIT:
            position = self.positions.get(intent.symbol)
            if position is not None:
                position.take_profit_order_id = order_state.local_order_id
        broker_order_id = await self.adapter.place_limit_order(
            local_order_id=order_state.local_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            purpose=intent.action.value,
            parent_local_order_id=intent.metadata.get("parent_local_order_id"),
        )
        order_state.broker_order_id = broker_order_id
        order_state.status = OrderStatus.SUBMITTED
        order_state.updated_at = _now()
        return [self._to_order_update(order_state, ts_event=order_state.updated_at)]

    def _shadow_execute(self, intent: TradeIntent, quote: QuoteUpdate | None) -> list[OrderUpdate | FillEvent]:
        submitted_at = _now()
        order_state = self._create_order_state(intent, OrderStatus.SIMULATED, submitted_at=submitted_at)
        self.orders[order_state.local_order_id] = order_state
        if intent.action == IntentAction.PLACE_TAKE_PROFIT:
            position = self.positions.get(intent.symbol)
            if position is not None:
                position.take_profit_order_id = order_state.local_order_id
        updates: list[OrderUpdate | FillEvent] = [self._to_order_update(order_state, ts_event=submitted_at)]
        if quote is None:
            return updates
        fill_price = self._shadow_fill_price(intent, quote)
        if fill_price is None:
            return updates
        updates.extend(self._fill_shadow_order(order_state, fill_price, submitted_at))
        return updates

    def _shadow_fill_price(self, intent: TradeIntent, quote: QuoteUpdate) -> float | None:
        if intent.action == IntentAction.PLACE_TAKE_PROFIT:
            return None
        if intent.entry_regime == EntryRegime.PASSIVE_IMPROVEMENT and intent.action in {IntentAction.OPEN_LONG, IntentAction.OPEN_SHORT}:
            return None
        if intent.side == TradeSide.BUY and intent.limit_price is not None and intent.limit_price >= quote.ask_price:
            return quote.ask_price
        if intent.side == TradeSide.SELL and intent.limit_price is not None and intent.limit_price <= quote.bid_price:
            return quote.bid_price
        return None

    def simulate_passive_orders(self, quote: QuoteUpdate) -> list[FillEvent | OrderUpdate]:
        if self.config.mode != EngineMode.SHADOW:
            return []
        generated: list[FillEvent | OrderUpdate] = []
        for order in list(self.orders.values()):
            if order.symbol != quote.symbol or order.status not in WORKING_ORDER_STATUSES:
                continue
            if order.limit_price is None:
                continue
            if order.purpose == IntentAction.PLACE_TAKE_PROFIT.value:
                if order.side == TradeSide.SELL and quote.bid_price >= order.limit_price:
                    generated.extend(self._fill_shadow_order(order, order.limit_price, _now()))
                elif order.side == TradeSide.BUY and quote.ask_price <= order.limit_price:
                    generated.extend(self._fill_shadow_order(order, order.limit_price, _now()))
            elif order.entry_regime == EntryRegime.PASSIVE_IMPROVEMENT and order.purpose in {
                IntentAction.OPEN_LONG.value,
                IntentAction.OPEN_SHORT.value,
            }:
                if order.side == TradeSide.BUY and quote.ask_price <= order.limit_price:
                    generated.extend(self._fill_shadow_order(order, order.limit_price, _now()))
                elif order.side == TradeSide.SELL and quote.bid_price >= order.limit_price:
                    generated.extend(self._fill_shadow_order(order, order.limit_price, _now()))
        return generated

    def cancel_expired_entries(self, now: datetime) -> list[OrderUpdate]:
        generated: list[OrderUpdate] = []
        for order in self.orders.values():
            if order.status not in WORKING_ORDER_STATUSES:
                continue
            if order.reduce_only:
                continue
            if order.entry_regime != EntryRegime.PASSIVE_IMPROVEMENT:
                continue
            if order.ttl_ms is None:
                continue
            expires_at = order.submitted_at + timedelta(milliseconds=order.ttl_ms)
            if now < expires_at:
                continue
            if order.cancel_reason:
                continue
            order.cancel_reason = "entry_ttl_expired"
            order.status = OrderStatus.CANCELED
            order.updated_at = now
            self.passive_entry_retries[order.symbol] = self.passive_entry_retries.get(order.symbol, 0) + 1
            generated.append(self._to_order_update(order, ts_event=now))
        return generated

    async def _cancel_take_profit_orders(self, symbol: str, reason: str) -> list[OrderUpdate]:
        generated: list[OrderUpdate] = []
        for order in list(self.orders.values()):
            if order.symbol != symbol:
                continue
            if order.status not in WORKING_ORDER_STATUSES:
                continue
            if order.purpose != IntentAction.PLACE_TAKE_PROFIT.value:
                continue
            update = await self._cancel_order_state(order, reason)
            if update is not None:
                generated.append(update)
        return generated

    async def _cancel_target_order(self, intent: TradeIntent, reason: str) -> OrderUpdate | None:
        target_local_order_id = str(intent.metadata.get("target_local_order_id", "") or "")
        target = self.orders.get(target_local_order_id)
        if target is None and target_local_order_id:
            return None
        if target is None:
            for order in self.orders.values():
                if order.symbol == intent.symbol and order.status in WORKING_ORDER_STATUSES:
                    target = order
                    break
        if target is None:
            return None
        return await self._cancel_order_state(target, reason)

    async def _cancel_order_state(self, order: OrderState, reason: str) -> OrderUpdate | None:
        if order.status not in WORKING_ORDER_STATUSES:
            return None
        if self.config.mode == EngineMode.LIVE and self.adapter is not None and order.broker_order_id is not None:
            await self.adapter.cancel_order(order.broker_order_id)
        order.status = OrderStatus.CANCELED
        order.cancel_reason = reason
        order.updated_at = _now()
        if order.purpose == IntentAction.PLACE_TAKE_PROFIT.value:
            position = self.positions.get(order.symbol)
            if position is not None and position.take_profit_order_id == order.local_order_id:
                position.take_profit_order_id = None
        return self._to_order_update(order, ts_event=order.updated_at)

    def _fill_shadow_order(self, order: OrderState, fill_price: float, ts_event: datetime) -> list[FillEvent | OrderUpdate]:
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.remaining_quantity = 0
        order.last_fill_price = fill_price
        order.updated_at = ts_event
        return [
            FillEvent(
                local_order_id=order.local_order_id,
                symbol=order.symbol,
                side=order.side,
                fill_price=fill_price,
                fill_size=order.quantity,
                ts_event=ts_event,
                ts_local=ts_event,
                liquidity="simulated",
                metadata={"purpose": order.purpose},
            ),
            self._to_order_update(order, ts_event=ts_event),
        ]

    def _create_order_state(self, intent: TradeIntent, status: OrderStatus, submitted_at: datetime | None = None) -> OrderState:
        submitted_at = submitted_at or _now()
        retry_count = self.passive_entry_retries.get(intent.symbol, 0) if intent.entry_regime == EntryRegime.PASSIVE_IMPROVEMENT else 0
        return OrderState(
            local_order_id=str(uuid4()),
            symbol=intent.symbol,
            side=intent.side,
            status=status,
            quantity=intent.quantity,
            filled_quantity=0,
            remaining_quantity=intent.quantity,
            limit_price=intent.limit_price,
            submitted_at=submitted_at,
            updated_at=submitted_at,
            take_profit_price=intent.take_profit_price,
            parent_local_order_id=intent.metadata.get("parent_local_order_id"),
            purpose=intent.action.value,
            purpose_detail=intent.purpose_detail,
            entry_regime=intent.entry_regime,
            ttl_ms=intent.ttl_ms,
            reduce_only=intent.reduce_only,
            reason=intent.reason,
            retry_count=retry_count,
            metadata=dict(intent.metadata),
        )

    def _to_order_update(self, order: OrderState, ts_event: datetime) -> OrderUpdate:
        return OrderUpdate(
            local_order_id=order.local_order_id,
            symbol=order.symbol,
            side=order.side,
            status=order.status,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
            limit_price=order.limit_price,
            ts_event=ts_event,
            ts_local=order.updated_at,
            broker_order_id=order.broker_order_id,
            parent_local_order_id=order.parent_local_order_id,
            reason=order.reason,
            purpose=order.purpose,
            purpose_detail=order.purpose_detail,
            entry_regime=order.entry_regime,
            ttl_ms=order.ttl_ms,
            cancel_reason=order.cancel_reason,
            reduce_only=order.reduce_only,
        )

    def apply_order_update(self, update: OrderUpdate) -> None:
        order = self.orders.get(update.local_order_id)
        if order is None:
            order = OrderState(
                local_order_id=update.local_order_id,
                symbol=update.symbol,
                side=update.side,
                status=update.status,
                quantity=update.quantity,
                filled_quantity=update.filled_quantity,
                remaining_quantity=update.remaining_quantity,
                limit_price=update.limit_price,
                submitted_at=update.ts_event,
                updated_at=update.ts_local,
                broker_order_id=update.broker_order_id,
                parent_local_order_id=update.parent_local_order_id,
                reason=update.reason,
                purpose=update.purpose,
                purpose_detail=update.purpose_detail,
                entry_regime=update.entry_regime,
                ttl_ms=update.ttl_ms,
                cancel_reason=update.cancel_reason,
                reduce_only=update.reduce_only,
            )
            self.orders[update.local_order_id] = order
            return
        order.status = update.status
        order.filled_quantity = update.filled_quantity
        order.remaining_quantity = update.remaining_quantity
        order.updated_at = update.ts_local
        order.broker_order_id = update.broker_order_id
        order.reason = update.reason
        order.purpose = update.purpose or order.purpose
        order.purpose_detail = update.purpose_detail or order.purpose_detail
        order.entry_regime = update.entry_regime
        order.ttl_ms = update.ttl_ms
        order.cancel_reason = update.cancel_reason or order.cancel_reason
        order.reduce_only = update.reduce_only

    def apply_fill(self, fill: FillEvent) -> list[TradeIntent]:
        follow_ups: list[TradeIntent] = []
        order = self.orders.get(fill.local_order_id)
        if order is not None:
            order.filled_quantity += fill.fill_size
            order.remaining_quantity = max(order.quantity - order.filled_quantity, 0)
            order.last_fill_price = fill.fill_price
            order.updated_at = fill.ts_local
            order.status = OrderStatus.FILLED if order.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED

        position = self.positions.get(fill.symbol)
        purpose = order.purpose if order else ""
        if purpose in {IntentAction.PLACE_TAKE_PROFIT.value, IntentAction.EXIT.value, IntentAction.FLATTEN.value}:
            if position is not None:
                self._close_position(position, fill)
            return follow_ups

        if position is None or position.quantity == 0 or position.side == PositionSide.FLAT:
            position = self._open_position(fill, order)
            self.positions[fill.symbol] = position
        else:
            self._increase_position(position, fill)

        if order is not None and order.entry_regime == EntryRegime.PASSIVE_IMPROVEMENT:
            self.passive_entry_retries[fill.symbol] = 0
        if purpose in {IntentAction.OPEN_LONG.value, IntentAction.OPEN_SHORT.value}:
            follow_ups.extend(self._rebuild_take_profit(position, order, fill.ts_event))
        return follow_ups

    def _open_position(self, fill: FillEvent, order: OrderState | None) -> PositionState:
        side = PositionSide.LONG if fill.side == TradeSide.BUY else PositionSide.SHORT
        return PositionState(
            symbol=fill.symbol,
            side=side,
            quantity=fill.fill_size,
            avg_price=fill.fill_price,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            opened_at=fill.ts_local,
            updated_at=fill.ts_local,
            entry_order_id=fill.local_order_id,
            take_profit_order_id=None,
            entry_regime=EntryRegime.NONE if order is None else order.entry_regime,
        )

    def _increase_position(self, position: PositionState, fill: FillEvent) -> None:
        total_cost = (position.avg_price * position.quantity) + (fill.fill_price * fill.fill_size)
        position.quantity += fill.fill_size
        position.avg_price = total_cost / max(position.quantity, 1)
        position.updated_at = fill.ts_local

    def _close_position(self, position: PositionState, fill: FillEvent) -> None:
        fill_size = min(fill.fill_size, position.quantity)
        if position.side == PositionSide.LONG:
            realized = (fill.fill_price - position.avg_price) * fill_size
        else:
            realized = (position.avg_price - fill.fill_price) * fill_size
        position.realized_pnl += realized
        position.quantity = max(position.quantity - fill_size, 0)
        position.updated_at = fill.ts_local
        if position.quantity == 0:
            position.side = PositionSide.FLAT
            position.take_profit_order_id = None

    def _rebuild_take_profit(self, position: PositionState, order: OrderState | None, ts_event: datetime) -> list[TradeIntent]:
        follow_ups: list[TradeIntent] = []
        if position.quantity <= 0 or position.side == PositionSide.FLAT:
            return follow_ups
        if position.take_profit_order_id is not None:
            existing_tp = self.orders.get(position.take_profit_order_id)
            if existing_tp is not None and existing_tp.status in WORKING_ORDER_STATUSES:
                follow_ups.append(
                    TradeIntent(
                        action=IntentAction.CANCEL,
                        symbol=position.symbol,
                        side=TradeSide.SELL if position.side == PositionSide.LONG else TradeSide.BUY,
                        quantity=0,
                        limit_price=None,
                        ts_event=ts_event,
                        reason="reprice_take_profit",
                        reduce_only=True,
                        purpose_detail="cancel_take_profit",
                        metadata={"target_local_order_id": existing_tp.local_order_id},
                    )
                )
        follow_ups.append(self._make_take_profit_intent(position, order, ts_event))
        return follow_ups

    def _make_take_profit_intent(self, position: PositionState, order: OrderState | None, ts_event: datetime) -> TradeIntent:
        tick_size = self.config.symbol_config(position.symbol).tick_size
        tp_ticks = float(order.metadata.get("tp_ticks", self.config.strategy.tp_ticks)) if order is not None else self.config.strategy.tp_ticks
        if position.side == PositionSide.LONG:
            return TradeIntent(
                action=IntentAction.PLACE_TAKE_PROFIT,
                symbol=position.symbol,
                side=TradeSide.SELL,
                quantity=position.quantity,
                limit_price=position.avg_price + (tp_ticks * tick_size),
                ts_event=ts_event,
                reason="entry_filled_take_profit",
                entry_regime=position.entry_regime,
                reduce_only=True,
                purpose_detail="take_profit",
                metadata={"parent_local_order_id": position.entry_order_id},
            )
        return TradeIntent(
            action=IntentAction.PLACE_TAKE_PROFIT,
            symbol=position.symbol,
            side=TradeSide.BUY,
            quantity=position.quantity,
            limit_price=position.avg_price - (tp_ticks * tick_size),
            ts_event=ts_event,
            reason="entry_filled_take_profit",
            entry_regime=position.entry_regime,
            reduce_only=True,
            purpose_detail="take_profit",
            metadata={"parent_local_order_id": position.entry_order_id},
        )

    def update_unrealized(self, quote: QuoteUpdate) -> None:
        position = self.positions.get(quote.symbol)
        if position is None or position.quantity == 0:
            return
        if position.side == PositionSide.LONG:
            position.unrealized_pnl = (quote.bid_price - position.avg_price) * position.quantity
        elif position.side == PositionSide.SHORT:
            position.unrealized_pnl = (position.avg_price - quote.ask_price) * position.quantity
        position.updated_at = quote.ts_local

    def pending_orders(self, symbol: str) -> int:
        return sum(1 for order in self.orders.values() if order.symbol == symbol and order.status in WORKING_ORDER_STATUSES)

    def passive_retry_available(self, symbol: str) -> bool:
        if any(
            order.symbol == symbol
            and order.entry_regime == EntryRegime.PASSIVE_IMPROVEMENT
            and order.status in WORKING_ORDER_STATUSES
            for order in self.orders.values()
        ):
            return False
        return self.passive_entry_retries.get(symbol, 0) < self.config.strategy.entry_regime_defaults.passive_entry_max_retries

    def position_for(self, symbol: str) -> PositionState | None:
        position = self.positions.get(symbol)
        if position and position.quantity == 0:
            return None
        return position

    def open_positions_count(self) -> int:
        return sum(1 for position in self.positions.values() if position.quantity > 0 and position.side != PositionSide.FLAT)
