from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import fmean, pstdev
from typing import TypeVar

from .types import BookUpdate, MarketDataCapabilities, MarketMetaUpdate, QuoteUpdate, TradePrint


T = TypeVar("T")


def _prune_time_window(items: deque[tuple[datetime, T]], cutoff: datetime) -> None:
    while items and items[0][0] < cutoff:
        items.popleft()


@dataclass(slots=True)
class SymbolMarketState:
    symbol: str
    quote: QuoteUpdate | None = None
    book: BookUpdate | None = None
    last_trade: TradePrint | None = None
    recent_trades: deque[TradePrint] = field(default_factory=deque)
    tape_ofi_window: deque[tuple[datetime, float]] = field(default_factory=deque)
    trade_burst_window: deque[tuple[datetime, float]] = field(default_factory=deque)
    quote_ofi_window: deque[tuple[datetime, float]] = field(default_factory=deque)
    depth_ofi_window: deque[tuple[datetime, float]] = field(default_factory=deque)
    microprice_window: deque[tuple[datetime, float]] = field(default_factory=deque)
    metric_history: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    shortable_tier: float | None = None
    shortable_shares: int | None = None
    rt_volume: float | None = None
    rt_trade_volume: float | None = None
    seen_tick_by_tick_bidask: bool = False
    seen_tick_by_tick_trades: bool = False
    seen_generic_ticks: bool = False
    last_quote_source: str = ""

    def rolling_zscore(self, metric: str, value: float, maxlen: int) -> float:
        series = self.metric_history.setdefault(metric, deque(maxlen=maxlen))
        if len(series) < 5:
            series.append(value)
            return 0.0
        mean = fmean(series)
        stdev = pstdev(series) or 1e-9
        zscore = (value - mean) / stdev
        series.append(value)
        return zscore

    def update_quote(self, quote: QuoteUpdate) -> QuoteUpdate | None:
        previous = self.quote
        self.quote = quote
        self.last_quote_source = quote.source
        if "tick_by_tick" in quote.source or "bidask" in quote.source:
            self.seen_tick_by_tick_bidask = True
        return previous

    def update_book(self, book: BookUpdate) -> QuoteUpdate | None:
        previous = self.quote
        self.book = book
        if book.bids and book.asks:
            best_bid = book.bids[0]
            best_ask = book.asks[0]
            self.quote = QuoteUpdate(
                symbol=book.symbol,
                ts_event=book.ts_event,
                ts_local=book.ts_local,
                bid_price=best_bid[0],
                bid_size=best_bid[1],
                ask_price=best_ask[0],
                ask_size=best_ask[1],
                source=book.source,
                is_stale=book.is_stale,
            )
        return previous

    def update_trade(self, trade: TradePrint, trade_window_ms: int) -> None:
        self.last_trade = trade
        self.recent_trades.append(trade)
        if "alllast" in trade.source or "tick_by_tick" in trade.source:
            self.seen_tick_by_tick_trades = True
        cutoff = trade.ts_event - timedelta(milliseconds=trade_window_ms)
        while self.recent_trades and self.recent_trades[0].ts_event < cutoff:
            self.recent_trades.popleft()

    def update_meta(self, meta: MarketMetaUpdate) -> None:
        if meta.shortable_tier is not None:
            self.shortable_tier = meta.shortable_tier
        if meta.shortable_shares is not None:
            self.shortable_shares = meta.shortable_shares
        if meta.rt_volume is not None:
            self.rt_volume = meta.rt_volume
        if meta.rt_trade_volume is not None:
            self.rt_trade_volume = meta.rt_trade_volume
        self.seen_generic_ticks = True

    def push_tape_ofi(self, ts_event: datetime, value: float, window_ms: int) -> None:
        self.tape_ofi_window.append((ts_event, value))
        _prune_time_window(self.tape_ofi_window, ts_event - timedelta(milliseconds=window_ms))

    def push_trade_burst(self, ts_event: datetime, value: float, window_ms: int) -> None:
        self.trade_burst_window.append((ts_event, value))
        _prune_time_window(self.trade_burst_window, ts_event - timedelta(milliseconds=window_ms))

    def push_quote_ofi(self, ts_event: datetime, value: float, window_ms: int) -> None:
        self.quote_ofi_window.append((ts_event, value))
        _prune_time_window(self.quote_ofi_window, ts_event - timedelta(milliseconds=window_ms))

    def push_depth_ofi(self, ts_event: datetime, value: float, window_ms: int) -> None:
        self.depth_ofi_window.append((ts_event, value))
        _prune_time_window(self.depth_ofi_window, ts_event - timedelta(milliseconds=window_ms))

    def push_microprice(self, ts_event: datetime, value: float, window_ms: int) -> None:
        self.microprice_window.append((ts_event, value))
        _prune_time_window(self.microprice_window, ts_event - timedelta(milliseconds=window_ms))

    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(
            tick_by_tick_bidask=self.seen_tick_by_tick_bidask,
            tick_by_tick_trades=self.seen_tick_by_tick_trades,
            depth_available=self.book is not None and bool(self.book.bids and self.book.asks),
            shortable_data_available=self.shortable_tier is not None or self.shortable_shares is not None,
            generic_ticks_available=self.seen_generic_ticks,
        )


class MarketStateStore:
    def __init__(self) -> None:
        self._states: dict[str, SymbolMarketState] = {}

    def state_for(self, symbol: str) -> SymbolMarketState:
        if symbol not in self._states:
            self._states[symbol] = SymbolMarketState(symbol=symbol)
        return self._states[symbol]

    def all_states(self) -> dict[str, SymbolMarketState]:
        return self._states
