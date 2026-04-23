from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Thread
from typing import Any

from .base import AbstractBrokerAdapter, EventPublisher
from ..config import EngineConfig
from ..types import (
    BookUpdate,
    BrokerSnapshot,
    FillEvent,
    MarketMetaUpdate,
    OrderState,
    OrderStatus,
    OrderUpdate,
    PositionSide,
    PositionState,
    QuoteUpdate,
    StatusUpdate,
    TradePrint,
    TradeSide,
)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


@dataclass(slots=True)
class _QuoteAccumulator:
    bid_price: float | None = None
    bid_size: float | None = None
    ask_price: float | None = None
    ask_size: float | None = None
    bids: dict[int, tuple[float, float]] = field(default_factory=dict)
    asks: dict[int, tuple[float, float]] = field(default_factory=dict)


class IBKRAdapter(AbstractBrokerAdapter):
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.publisher: EventPublisher | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._app: Any | None = None
        self._thread: Thread | None = None
        self._next_order_id: int | None = None
        self._order_id_ready = asyncio.Event()
        self._req_id_counter = 10_000
        self._req_id_to_symbol: dict[int, str] = {}
        self._quote_state: dict[str, _QuoteAccumulator] = defaultdict(_QuoteAccumulator)
        self._positions: dict[str, PositionState] = {}
        self._open_orders: dict[str, OrderState] = {}
        self._order_lookup: dict[int, dict[str, Any]] = {}
        self._reconnect_count = 0

    def set_publisher(self, publisher: EventPublisher) -> None:
        self.publisher = publisher

    async def connect(self) -> None:
        try:
            from ibapi.client import EClient
            from ibapi.common import TickerId
            from ibapi.contract import Contract
            from ibapi.order import Order
            from ibapi.ticktype import TickTypeEnum
            from ibapi.wrapper import EWrapper
        except ModuleNotFoundError as exc:
            raise RuntimeError("ibapi is required for the IBKR adapter") from exc

        self.loop = asyncio.get_running_loop()
        adapter = self

        class App(EWrapper, EClient):
            def __init__(self) -> None:
                EClient.__init__(self, self)

            def nextValidId(self, orderId: int) -> None:  # noqa: N802
                adapter._next_order_id = orderId
                if adapter.loop is not None:
                    adapter.loop.call_soon_threadsafe(adapter._order_id_ready.set)

            def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
                adapter._publish_threadsafe(
                    StatusUpdate(
                        symbol=adapter._req_id_to_symbol.get(reqId, ""),
                        ts_event=_utc_now(),
                        ts_local=_utc_now(),
                        status="error",
                        detail=errorString,
                        code=errorCode,
                        metadata={"advanced_reject": advancedOrderRejectJson},
                    )
                )

            def connectionClosed(self) -> None:  # noqa: N802
                adapter._reconnect_count += 1
                adapter._publish_threadsafe(
                    StatusUpdate(
                        symbol="",
                        ts_event=_utc_now(),
                        ts_local=_utc_now(),
                        status="connection_closed",
                        detail="IBKR connection closed",
                        metadata={"reconnect_count": adapter._reconnect_count},
                    )
                )

            def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib: Any) -> None:  # noqa: N802
                symbol = adapter._req_id_to_symbol.get(reqId)
                if not symbol:
                    return
                state = adapter._quote_state[symbol]
                if tickType == TickTypeEnum.BID:
                    state.bid_price = price
                elif tickType == TickTypeEnum.ASK:
                    state.ask_price = price
                elif tickType == TickTypeEnum.LAST:
                    adapter._publish_trade(symbol, price=price, size=0.0, source="ibkr_top_of_book_last")
                    return
                adapter._emit_quote(symbol, source="ibkr_top_of_book")

            def tickSize(self, reqId: TickerId, tickType: int, size: int) -> None:  # noqa: N802
                symbol = adapter._req_id_to_symbol.get(reqId)
                if not symbol:
                    return
                state = adapter._quote_state[symbol]
                if tickType == TickTypeEnum.BID_SIZE:
                    state.bid_size = float(size)
                elif tickType == TickTypeEnum.ASK_SIZE:
                    state.ask_size = float(size)
                adapter._emit_quote(symbol, source="ibkr_top_of_book")

            def tickGeneric(self, reqId: TickerId, tickType: int, value: float) -> None:  # noqa: N802
                symbol = adapter._req_id_to_symbol.get(reqId)
                if not symbol:
                    return
                payload: dict[str, Any] = {}
                if tickType == 46:
                    payload["shortable_tier"] = value
                elif tickType == 89:
                    payload["shortable_shares"] = int(value)
                else:
                    return
                adapter._publish_meta(symbol, **payload)

            def tickString(self, reqId: TickerId, tickType: int, value: str) -> None:  # noqa: N802
                symbol = adapter._req_id_to_symbol.get(reqId)
                if not symbol:
                    return
                if tickType not in {48, 77}:
                    return
                rt_volume, rt_trade_volume = adapter._parse_rt_volume(value)
                adapter._publish_meta(
                    symbol,
                    rt_volume=rt_volume,
                    rt_trade_volume=rt_trade_volume,
                    metadata={"tick_type": tickType},
                )

            def tickByTickBidAsk(  # noqa: N802
                self,
                reqId: int,
                time: int,
                bidPrice: float,
                askPrice: float,
                bidSize: float,
                askSize: float,
                tickAttribBidAsk: Any,
            ) -> None:
                symbol = adapter._req_id_to_symbol.get(reqId)
                if not symbol:
                    return
                state = adapter._quote_state[symbol]
                state.bid_price = bidPrice
                state.ask_price = askPrice
                state.bid_size = float(bidSize)
                state.ask_size = float(askSize)
                adapter._emit_quote(symbol, source="ibkr_tick_by_tick_bidask")

            def updateMktDepthL2(  # noqa: N802
                self,
                reqId: int,
                position: int,
                marketMaker: str,
                operation: int,
                side: int,
                price: float,
                size: int,
                isSmartDepth: bool,
            ) -> None:
                symbol = adapter._req_id_to_symbol.get(reqId)
                if not symbol:
                    return
                state = adapter._quote_state[symbol]
                ladder = state.asks if side else state.bids
                if operation == 2:
                    ladder.pop(position, None)
                else:
                    ladder[position] = (price, float(size))
                adapter._emit_book(symbol)

            def updateMktDepth(  # noqa: N802
                self,
                reqId: int,
                position: int,
                operation: int,
                side: int,
                price: float,
                size: int,
            ) -> None:
                self.updateMktDepthL2(reqId, position, "", operation, side, price, size, False)

            def tickByTickAllLast(  # noqa: N802
                self,
                reqId: int,
                tickType: int,
                time: int,
                price: float,
                size: int,
                tickAttribLast: Any,
                exchange: str,
                specialConditions: str,
            ) -> None:
                symbol = adapter._req_id_to_symbol.get(reqId)
                if not symbol:
                    return
                adapter._publish_trade(
                    symbol,
                    price=price,
                    size=float(size),
                    exchange=exchange,
                    sequence=time,
                    source="ibkr_tick_by_tick_alllast",
                )

            def orderStatus(  # noqa: N802
                self,
                orderId: int,
                status: str,
                filled: float,
                remaining: float,
                avgFillPrice: float,
                permId: int,
                parentId: int,
                lastFillPrice: float,
                clientId: int,
                whyHeld: str,
                mktCapPrice: float,
            ) -> None:
                lookup = adapter._order_lookup.get(orderId, {})
                symbol = str(lookup.get("symbol", ""))
                side = lookup.get("side", TradeSide.BUY)
                local_order_id = str(lookup.get("local_order_id", orderId))
                previous_filled = int(lookup.get("last_filled_quantity", 0))
                adapter._publish_threadsafe(
                    OrderUpdate(
                        local_order_id=local_order_id,
                        symbol=symbol,
                        side=side,
                        status=adapter._map_order_status(status),
                        quantity=int(filled + remaining),
                        filled_quantity=int(filled),
                        remaining_quantity=int(remaining),
                        limit_price=lookup.get("limit_price"),
                        ts_event=_utc_now(),
                        ts_local=_utc_now(),
                        broker_order_id=orderId,
                        reason=whyHeld,
                        purpose=str(lookup.get("purpose", "")),
                    )
                )
                filled_delta = max(int(filled) - previous_filled, 0)
                adapter._order_lookup.setdefault(orderId, {})["last_filled_quantity"] = int(filled)
                if lastFillPrice > 0 and filled_delta > 0:
                    adapter._publish_threadsafe(
                        FillEvent(
                            local_order_id=local_order_id,
                            symbol=symbol,
                            side=side,
                            fill_price=lastFillPrice,
                            fill_size=filled_delta,
                            ts_event=_utc_now(),
                            ts_local=_utc_now(),
                            broker_order_id=orderId,
                        )
                    )

            def position(self, account: str, contract: Any, position: float, avgCost: float) -> None:  # noqa: N802
                side = PositionSide.FLAT
                if position > 0:
                    side = PositionSide.LONG
                elif position < 0:
                    side = PositionSide.SHORT
                adapter._positions[contract.symbol] = PositionState(
                    symbol=contract.symbol,
                    side=side,
                    quantity=abs(int(position)),
                    avg_price=avgCost,
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    opened_at=None,
                    updated_at=_utc_now(),
                )

            def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:  # noqa: N802
                lookup = adapter._order_lookup.get(orderId, {})
                local_order_id = str(lookup.get("local_order_id", orderId))
                side = lookup.get("side", TradeSide.BUY if order.action.upper() == "BUY" else TradeSide.SELL)
                adapter._open_orders[str(orderId)] = OrderState(
                    local_order_id=local_order_id,
                    broker_order_id=orderId,
                    symbol=contract.symbol,
                    side=side,
                    status=adapter._map_order_status(orderState.status),
                    quantity=int(order.totalQuantity),
                    filled_quantity=0,
                    remaining_quantity=int(order.totalQuantity),
                    limit_price=getattr(order, "lmtPrice", None),
                    submitted_at=_utc_now(),
                    updated_at=_utc_now(),
                    purpose=getattr(order, "orderRef", ""),
                )

        self._contract_cls = Contract
        self._order_cls = Order
        self._app = App()
        self._app.connect(self.config.ibkr.host, self.config.ibkr.port, self.config.ibkr.client_id)
        self._thread = Thread(target=self._app.run, name="ibkr-api", daemon=True)
        self._thread.start()
        await asyncio.wait_for(self._order_id_ready.wait(), timeout=15)
        self._app.reqMarketDataType(self.config.ibkr.market_data_type)

    async def disconnect(self) -> None:
        if self._app is not None:
            self._app.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=2)

    async def subscribe_market_data(self, symbols: list[str], depth_levels: int) -> None:
        if self._app is None:
            raise RuntimeError("adapter not connected")
        for symbol in symbols:
            contract = self._make_contract(symbol)

            mkt_req_id = self._next_request_id(symbol)
            self._app.reqMktData(mkt_req_id, contract, self.config.ibkr.generic_ticks, False, False, [])

            bidask_req_id = self._next_request_id(symbol)
            self._app.reqTickByTickData(bidask_req_id, contract, "BidAsk", 0, True)

            all_last_req_id = self._next_request_id(symbol)
            self._app.reqTickByTickData(all_last_req_id, contract, "AllLast", 0, True)

            if self.config.ibkr.subscribe_depth and not self.config.ibkr.snapshot_permissions_only:
                depth_req_id = self._next_request_id(symbol)
                self._app.reqMktDepth(depth_req_id, contract, depth_levels, False, [])

        self._publish_threadsafe(
            StatusUpdate(
                symbol="",
                ts_event=_utc_now(),
                ts_local=_utc_now(),
                status="subscribed",
                detail="market data subscriptions active",
                metadata={"symbols": symbols, "depth_levels": depth_levels},
            )
        )

    async def place_limit_order(
        self,
        local_order_id: str,
        symbol: str,
        side: TradeSide,
        quantity: int,
        limit_price: float | None,
        purpose: str,
        parent_local_order_id: str | None = None,
        outside_rth: bool = False,
    ) -> int:
        if self._app is None or self._next_order_id is None:
            raise RuntimeError("adapter not connected")
        order_id = self._next_order_id
        self._next_order_id += 1
        contract = self._make_contract(symbol)
        order = self._order_cls()
        order.orderId = order_id
        order.action = "BUY" if side == TradeSide.BUY else "SELL"
        order.totalQuantity = quantity
        order.orderType = "LMT"
        order.lmtPrice = limit_price or 0.0
        order.tif = "DAY"
        order.outsideRth = outside_rth
        order.orderRef = purpose
        if parent_local_order_id:
            parent_lookup = next(
                (broker_id for broker_id, payload in self._order_lookup.items() if payload["local_order_id"] == parent_local_order_id),
                None,
            )
            if parent_lookup is not None:
                order.parentId = int(parent_lookup)
        self._order_lookup[order_id] = {
            "local_order_id": local_order_id,
            "symbol": symbol,
            "side": side,
            "limit_price": limit_price,
            "purpose": purpose,
            "last_filled_quantity": 0,
        }
        self._app.placeOrder(order_id, contract, order)
        return order_id

    async def cancel_order(self, broker_order_id: int) -> None:
        if self._app is None:
            raise RuntimeError("adapter not connected")
        self._app.cancelOrder(broker_order_id, "")

    async def request_reconcile(self) -> BrokerSnapshot:
        if self._app is None:
            raise RuntimeError("adapter not connected")
        self._positions.clear()
        self._open_orders.clear()
        self._app.reqPositions()
        self._app.reqOpenOrders()
        await asyncio.sleep(1.0)
        return BrokerSnapshot(
            positions=list(self._positions.values()),
            open_orders=list(self._open_orders.values()),
            ts_local=_utc_now(),
        )

    def _next_request_id(self, symbol: str) -> int:
        self._req_id_counter += 1
        self._req_id_to_symbol[self._req_id_counter] = symbol
        return self._req_id_counter

    def _make_contract(self, symbol: str) -> Any:
        contract = self._contract_cls()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contract.primaryExchange = "NASDAQ"
        return contract

    def _emit_quote(self, symbol: str, source: str) -> None:
        state = self._quote_state[symbol]
        if None in {state.bid_price, state.bid_size, state.ask_price, state.ask_size}:
            return
        event = QuoteUpdate(
            symbol=symbol,
            ts_event=_utc_now(),
            ts_local=_utc_now(),
            bid_price=float(state.bid_price),
            bid_size=float(state.bid_size),
            ask_price=float(state.ask_price),
            ask_size=float(state.ask_size),
            source=source,
        )
        self._publish_threadsafe(event)

    def _emit_book(self, symbol: str) -> None:
        state = self._quote_state[symbol]
        bids = tuple(state.bids[index] for index in sorted(state.bids))
        asks = tuple(state.asks[index] for index in sorted(state.asks))
        if not bids or not asks:
            return
        self._publish_threadsafe(
            BookUpdate(
                symbol=symbol,
                ts_event=_utc_now(),
                ts_local=_utc_now(),
                bids=bids,
                asks=asks,
                source="ibkr_depth",
            )
        )

    def _publish_trade(
        self,
        symbol: str,
        price: float,
        size: float,
        exchange: str = "",
        sequence: int = 0,
        source: str = "ibkr",
    ) -> None:
        accumulator = self._quote_state[symbol]
        side = TradeSide.UNKNOWN
        if accumulator.ask_price is not None and price >= accumulator.ask_price:
            side = TradeSide.BUY
        elif accumulator.bid_price is not None and price <= accumulator.bid_price:
            side = TradeSide.SELL
        self._publish_threadsafe(
            TradePrint(
                symbol=symbol,
                ts_event=_utc_now(),
                ts_local=_utc_now(),
                price=price,
                size=size,
                side=side,
                exchange=exchange,
                sequence=sequence,
                source=source,
            )
        )

    def _publish_meta(self, symbol: str, **payload: Any) -> None:
        metadata = payload.pop("metadata", {})
        self._publish_threadsafe(
            MarketMetaUpdate(
                symbol=symbol,
                ts_event=_utc_now(),
                ts_local=_utc_now(),
                shortable_tier=payload.get("shortable_tier"),
                shortable_shares=payload.get("shortable_shares"),
                rt_volume=payload.get("rt_volume"),
                rt_trade_volume=payload.get("rt_trade_volume"),
                metadata=metadata,
                source="ibkr_generic_ticks",
            )
        )

    def _parse_rt_volume(self, value: str) -> tuple[float | None, float | None]:
        parts = value.split(";")
        if len(parts) < 4:
            return None, None
        try:
            total_volume = float(parts[3])
        except ValueError:
            total_volume = None
        trade_volume = None
        if len(parts) >= 2:
            try:
                trade_volume = float(parts[1])
            except ValueError:
                trade_volume = None
        return total_volume, trade_volume

    def _publish_threadsafe(self, event: Any) -> None:
        if self.publisher is None or self.loop is None:
            return
        self.loop.call_soon_threadsafe(asyncio.create_task, self.publisher(event))

    def _map_order_status(self, status: str) -> OrderStatus:
        normalized = status.lower()
        mapping = {
            "submitted": OrderStatus.SUBMITTED,
            "presubmitted": OrderStatus.SUBMITTED,
            "filled": OrderStatus.FILLED,
            "cancelled": OrderStatus.CANCELED,
            "pendingcancel": OrderStatus.PENDING_CANCEL,
            "inactive": OrderStatus.REJECTED,
        }
        return mapping.get(normalized, OrderStatus.NEW)
