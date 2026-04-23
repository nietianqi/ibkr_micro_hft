from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EngineMode(str, Enum):
    CAPTURE = "capture"
    SHADOW = "shadow"
    LIVE = "live"


class EventKind(str, Enum):
    BOOK = "book"
    QUOTE = "quote"
    TRADE = "trade"
    STATUS = "status"
    META = "meta"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class IntentAction(str, Enum):
    NOOP = "noop"
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    EXIT = "exit"
    PLACE_TAKE_PROFIT = "place_take_profit"
    FLATTEN = "flatten"
    CANCEL = "cancel"


class OrderStatus(str, Enum):
    NEW = "new"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    PENDING_CANCEL = "pending_cancel"
    SIMULATED = "simulated"


class EntryRegime(str, Enum):
    NONE = "none"
    CONFIRMED_TAKER = "confirmed_taker"
    PASSIVE_IMPROVEMENT = "passive_improvement"


@dataclass(slots=True)
class BookUpdate:
    symbol: str
    ts_event: datetime
    ts_local: datetime
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    source: str = "ibkr"
    kind: EventKind = EventKind.BOOK
    is_stale: bool = False


@dataclass(slots=True)
class QuoteUpdate:
    symbol: str
    ts_event: datetime
    ts_local: datetime
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    source: str = "ibkr"
    kind: EventKind = EventKind.QUOTE
    is_stale: bool = False

    @property
    def spread(self) -> float:
        return max(self.ask_price - self.bid_price, 0.0)

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0


@dataclass(slots=True)
class TradePrint:
    symbol: str
    ts_event: datetime
    ts_local: datetime
    price: float
    size: float
    side: TradeSide = TradeSide.UNKNOWN
    exchange: str = ""
    sequence: int = 0
    source: str = "ibkr"
    kind: EventKind = EventKind.TRADE
    is_stale: bool = False


@dataclass(slots=True)
class StatusUpdate:
    symbol: str
    ts_event: datetime
    ts_local: datetime
    status: str
    detail: str = ""
    code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "ibkr"
    kind: EventKind = EventKind.STATUS
    is_stale: bool = False


@dataclass(slots=True)
class MarketMetaUpdate:
    symbol: str
    ts_event: datetime
    ts_local: datetime
    shortable_tier: float | None = None
    shortable_shares: int | None = None
    rt_volume: float | None = None
    rt_trade_volume: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "ibkr"
    kind: EventKind = EventKind.META
    is_stale: bool = False


NormalizedEvent = BookUpdate | QuoteUpdate | TradePrint | StatusUpdate | MarketMetaUpdate


@dataclass(slots=True)
class OrderUpdate:
    local_order_id: str
    symbol: str
    side: TradeSide
    status: OrderStatus
    quantity: int
    filled_quantity: int
    remaining_quantity: int
    limit_price: float | None
    ts_event: datetime
    ts_local: datetime
    broker_order_id: int | None = None
    parent_local_order_id: str | None = None
    reason: str = ""
    purpose: str = ""
    purpose_detail: str = ""
    entry_regime: EntryRegime = EntryRegime.NONE
    ttl_ms: int | None = None
    cancel_reason: str = ""
    reduce_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FillEvent:
    local_order_id: str
    symbol: str
    side: TradeSide
    fill_price: float
    fill_size: int
    ts_event: datetime
    ts_local: datetime
    broker_order_id: int | None = None
    commission: float = 0.0
    liquidity: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


EngineEvent = NormalizedEvent | OrderUpdate | FillEvent


@dataclass(slots=True)
class MarketDataCapabilities:
    tick_by_tick_bidask: bool = False
    tick_by_tick_trades: bool = False
    depth_available: bool = False
    shortable_data_available: bool = False
    generic_ticks_available: bool = False


@dataclass(slots=True)
class SignalFilterState:
    market_ok: bool
    linkage_score: float
    overheat_long_ok: bool
    overheat_short_ok: bool
    quote_age_ms: float
    trade_rate_per_sec: float
    spread_ticks: float
    depth_available: bool
    short_inventory_ok: bool
    abnormal: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class SignalSnapshot:
    symbol: str
    ts_event: datetime
    ts_local: datetime
    weighted_imbalance: float
    lob_ofi: float
    l1_imbalance: float
    quote_ofi: float
    tape_ofi: float
    trade_burst: float
    microprice: float
    microprice_momentum: float
    microprice_tilt: float
    zscores: dict[str, float]
    filters: SignalFilterState
    long_score: float
    short_score: float
    depth_available: bool
    shortable_tier: float | None = None
    shortable_shares: int | None = None
    entry_regime_candidate: EntryRegime = EntryRegime.NONE
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderState:
    local_order_id: str
    symbol: str
    side: TradeSide
    status: OrderStatus
    quantity: int
    filled_quantity: int
    remaining_quantity: int
    limit_price: float | None
    submitted_at: datetime
    updated_at: datetime
    broker_order_id: int | None = None
    take_profit_price: float | None = None
    parent_local_order_id: str | None = None
    purpose: str = ""
    purpose_detail: str = ""
    entry_regime: EntryRegime = EntryRegime.NONE
    ttl_ms: int | None = None
    cancel_reason: str = ""
    reduce_only: bool = False
    last_fill_price: float | None = None
    reason: str = ""
    retry_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PositionState:
    symbol: str
    side: PositionSide
    quantity: int
    avg_price: float
    realized_pnl: float
    unrealized_pnl: float
    opened_at: datetime | None
    updated_at: datetime
    entry_order_id: str | None = None
    take_profit_order_id: str | None = None
    entry_regime: EntryRegime = EntryRegime.NONE


@dataclass(slots=True)
class SessionHealth:
    connected: bool
    data_stale: bool
    last_event_at: datetime | None
    reconnect_count: int
    kill_switch_engaged: bool
    pending_orders: int
    mode: EngineMode
    warnings: tuple[str, ...] = ()
    kill_switch_reason: str | None = None


@dataclass(slots=True)
class DecisionContext:
    symbol: str
    ts_event: datetime
    mode: EngineMode
    signal: SignalSnapshot | None
    quote: QuoteUpdate | None
    position: PositionState | None
    session_health: SessionHealth
    pending_orders: int
    depth_available: bool = False
    short_inventory_ok: bool = True
    shortable_tier: float | None = None
    shortable_shares: int | None = None
    market_data_capabilities: MarketDataCapabilities = field(default_factory=MarketDataCapabilities)
    passive_retry_available: bool = True


@dataclass(slots=True)
class TradeIntent:
    action: IntentAction
    symbol: str
    side: TradeSide
    quantity: int
    limit_price: float | None
    ts_event: datetime
    reason: str
    take_profit_price: float | None = None
    entry_regime: EntryRegime = EntryRegime.NONE
    max_slippage_ticks: float = 0.0
    reduce_only: bool = False
    ttl_ms: int | None = None
    purpose_detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    max_quantity: int
    kill_switch: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrokerSnapshot:
    positions: list[PositionState]
    open_orders: list[OrderState]
    ts_local: datetime
