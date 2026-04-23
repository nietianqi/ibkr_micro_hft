from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from statistics import fmean, pstdev

from .config import EngineConfig
from .state import MarketStateStore, SymbolMarketState
from .types import (
    BookUpdate,
    EntryRegime,
    MarketMetaUpdate,
    QuoteUpdate,
    SignalFilterState,
    SignalSnapshot,
    TradePrint,
    TradeSide,
)


def weighted_imbalance(bids: tuple[tuple[float, float], ...], asks: tuple[tuple[float, float], ...]) -> float:
    bid_total = 0.0
    ask_total = 0.0
    max_levels = max(len(bids), len(asks))
    for idx in range(max_levels):
        weight = 1.0 / (idx + 1)
        if idx < len(bids):
            bid_total += bids[idx][1] * weight
        if idx < len(asks):
            ask_total += asks[idx][1] * weight
    denominator = bid_total + ask_total
    if denominator <= 0:
        return 0.0
    return (bid_total - ask_total) / denominator


def microprice(quote: QuoteUpdate) -> float:
    denominator = quote.bid_size + quote.ask_size
    if denominator <= 0:
        return quote.mid_price
    return ((quote.ask_price * quote.bid_size) + (quote.bid_price * quote.ask_size)) / denominator


def classify_trade_side(trade: TradePrint, quote: QuoteUpdate | None) -> TradeSide:
    if trade.side != TradeSide.UNKNOWN:
        return trade.side
    if quote is None:
        return TradeSide.UNKNOWN
    if trade.price >= quote.ask_price:
        return TradeSide.BUY
    if trade.price <= quote.bid_price:
        return TradeSide.SELL
    if trade.price > quote.mid_price:
        return TradeSide.BUY
    if trade.price < quote.mid_price:
        return TradeSide.SELL
    return TradeSide.UNKNOWN


@dataclass(slots=True)
class SignalCalculator:
    config: EngineConfig
    market_states: MarketStateStore
    latest_signals: dict[str, SignalSnapshot] = field(default_factory=dict)

    def on_event(self, event: BookUpdate | QuoteUpdate | TradePrint | MarketMetaUpdate) -> SignalSnapshot | None:
        state = self.market_states.state_for(event.symbol)
        quote = self._update_state(state, event)
        if quote is None:
            return None

        raw_l1_imbalance = weighted_imbalance(
            ((quote.bid_price, quote.bid_size),),
            ((quote.ask_price, quote.ask_size),),
        )
        raw_quote_ofi = self._compute_quote_ofi(state)
        raw_tape_ofi = self._compute_tape_ofi(state)
        raw_trade_burst = self._compute_trade_burst(state)
        raw_microprice = microprice(quote)
        state.push_microprice(event.ts_event, raw_microprice, self.config.strategy.microprice_window_ms)
        raw_mp_momentum = self._compute_microprice_momentum(state)
        raw_mp_tilt = self._compute_microprice_tilt(quote, raw_microprice, state.symbol)
        raw_weighted_imbalance = self._compute_weighted_imbalance(state, quote)
        raw_lob_ofi = self._compute_depth_ofi(state)

        zscores = {
            "l1_imbalance": state.rolling_zscore("l1_imbalance", raw_l1_imbalance, self.config.strategy.signal_window),
            "quote_ofi": state.rolling_zscore("quote_ofi", raw_quote_ofi, self.config.strategy.signal_window),
            "tape_ofi": state.rolling_zscore("tape_ofi", raw_tape_ofi, self.config.strategy.signal_window),
            "trade_burst": state.rolling_zscore("trade_burst", raw_trade_burst, self.config.strategy.signal_window),
            "microprice_tilt": state.rolling_zscore("microprice_tilt", raw_mp_tilt, self.config.strategy.signal_window),
            "microprice_momentum": state.rolling_zscore(
                "microprice_momentum",
                raw_mp_momentum,
                self.config.strategy.signal_window,
            ),
            "weighted_imbalance": state.rolling_zscore(
                "weighted_imbalance",
                raw_weighted_imbalance,
                self.config.strategy.signal_window,
            ),
            "lob_ofi": state.rolling_zscore("lob_ofi", raw_lob_ofi, self.config.strategy.signal_window),
        }
        filters = self._build_filters(state, quote, event.ts_local)
        base_score = (
            self.config.strategy.weights.quote_ofi * zscores["quote_ofi"]
            + self.config.strategy.weights.tape_ofi * zscores["tape_ofi"]
            + self.config.strategy.weights.l1_imbalance * zscores["l1_imbalance"]
            + self.config.strategy.weights.trade_burst * zscores["trade_burst"]
            + self.config.strategy.weights.microprice_tilt * zscores["microprice_tilt"]
            + self.config.strategy.weights.microprice_momentum * zscores["microprice_momentum"]
        )
        linkage_adjustment = self.config.strategy.weights.linkage * filters.linkage_score
        depth_adjustment = 0.0
        if self.config.strategy.depth_bonus_enabled and filters.depth_available:
            depth_adjustment = self.config.strategy.weights.depth_bonus * (
                0.5 * zscores["weighted_imbalance"] + 0.5 * zscores["lob_ofi"]
            )
        long_score = base_score + linkage_adjustment + depth_adjustment
        short_score = (-base_score) - linkage_adjustment - depth_adjustment
        snapshot = SignalSnapshot(
            symbol=state.symbol,
            ts_event=event.ts_event,
            ts_local=event.ts_local,
            weighted_imbalance=raw_weighted_imbalance,
            lob_ofi=raw_lob_ofi,
            l1_imbalance=raw_l1_imbalance,
            quote_ofi=raw_quote_ofi,
            tape_ofi=raw_tape_ofi,
            trade_burst=raw_trade_burst,
            microprice=raw_microprice,
            microprice_momentum=raw_mp_momentum,
            microprice_tilt=raw_mp_tilt,
            zscores=zscores,
            filters=filters,
            long_score=long_score,
            short_score=short_score,
            depth_available=filters.depth_available,
            shortable_tier=state.shortable_tier,
            shortable_shares=state.shortable_shares,
            entry_regime_candidate=self._entry_regime_candidate(
                state=state,
                filters=filters,
                long_score=long_score,
                short_score=short_score,
            ),
            metadata={
                "mid_price": quote.mid_price,
                "market_data_capabilities": asdict(state.capabilities()),
            },
        )
        self.latest_signals[state.symbol] = snapshot
        return snapshot

    def _update_state(
        self,
        state: SymbolMarketState,
        event: BookUpdate | QuoteUpdate | TradePrint | MarketMetaUpdate,
    ) -> QuoteUpdate | None:
        if isinstance(event, BookUpdate):
            previous_book = state.book
            state.update_book(event)
            if previous_book is not None:
                state.push_depth_ofi(
                    event.ts_event,
                    self._depth_ofi_increment(previous_book, event),
                    self.config.strategy.depth_window_ms,
                )
        elif isinstance(event, QuoteUpdate):
            previous_quote = state.update_quote(event)
            if previous_quote is not None:
                state.push_quote_ofi(
                    event.ts_event,
                    self._quote_ofi_increment(previous_quote, event),
                    self.config.strategy.quote_window_ms,
                )
        elif isinstance(event, TradePrint):
            state.update_trade(event, self.config.strategy.trade_window_ms)
            trade_side = classify_trade_side(event, state.quote)
            signed_size = event.size if trade_side == TradeSide.BUY else (-event.size if trade_side == TradeSide.SELL else 0.0)
            state.push_tape_ofi(event.ts_event, signed_size, self.config.strategy.trade_window_ms)
            state.push_trade_burst(event.ts_event, event.size, self.config.strategy.trade_burst_window_ms)
        elif isinstance(event, MarketMetaUpdate):
            state.update_meta(event)
        return state.quote

    def _compute_weighted_imbalance(self, state: SymbolMarketState, quote: QuoteUpdate) -> float:
        if state.book and state.book.bids and state.book.asks:
            return weighted_imbalance(state.book.bids, state.book.asks)
        return weighted_imbalance(
            ((quote.bid_price, quote.bid_size),),
            ((quote.ask_price, quote.ask_size),),
        )

    def _quote_ofi_increment(self, previous_quote: QuoteUpdate, current_quote: QuoteUpdate) -> float:
        e_bid = (current_quote.bid_size if current_quote.bid_price >= previous_quote.bid_price else 0.0) - (
            previous_quote.bid_size if current_quote.bid_price <= previous_quote.bid_price else 0.0
        )
        e_ask = (current_quote.ask_size if current_quote.ask_price <= previous_quote.ask_price else 0.0) - (
            previous_quote.ask_size if current_quote.ask_price >= previous_quote.ask_price else 0.0
        )
        return e_bid - e_ask

    def _depth_ofi_increment(self, previous_book: BookUpdate, current_book: BookUpdate) -> float:
        previous_bid_total, previous_ask_total = self._weighted_depth_totals(previous_book)
        current_bid_total, current_ask_total = self._weighted_depth_totals(current_book)
        return (current_bid_total - previous_bid_total) - (current_ask_total - previous_ask_total)

    def _weighted_depth_totals(self, book: BookUpdate) -> tuple[float, float]:
        bid_total = 0.0
        ask_total = 0.0
        max_levels = max(len(book.bids), len(book.asks))
        for idx in range(max_levels):
            weight = 1.0 / (idx + 1)
            if idx < len(book.bids):
                bid_total += book.bids[idx][1] * weight
            if idx < len(book.asks):
                ask_total += book.asks[idx][1] * weight
        return bid_total, ask_total

    def _compute_quote_ofi(self, state: SymbolMarketState) -> float:
        return sum(value for _, value in state.quote_ofi_window)

    def _compute_depth_ofi(self, state: SymbolMarketState) -> float:
        return sum(value for _, value in state.depth_ofi_window)

    def _compute_tape_ofi(self, state: SymbolMarketState) -> float:
        return sum(value for _, value in state.tape_ofi_window)

    def _compute_trade_burst(self, state: SymbolMarketState) -> float:
        return sum(value for _, value in state.trade_burst_window)

    def _compute_microprice_momentum(self, state: SymbolMarketState) -> float:
        if len(state.microprice_window) < 2:
            return 0.0
        first_ts, first_value = state.microprice_window[0]
        last_ts, last_value = state.microprice_window[-1]
        elapsed = max((last_ts - first_ts).total_seconds(), 1e-9)
        return (last_value - first_value) / elapsed

    def _compute_microprice_tilt(self, quote: QuoteUpdate, microprice_value: float, symbol: str) -> float:
        tick_size = self.config.symbol_config(symbol).tick_size
        return (microprice_value - quote.mid_price) / tick_size if tick_size > 0 else 0.0

    def _build_filters(self, state: SymbolMarketState, quote: QuoteUpdate, now: datetime) -> SignalFilterState:
        symbol_config = self.config.symbol_config(state.symbol)
        tick_size = symbol_config.tick_size
        quote_age_ms = max((now - quote.ts_event).total_seconds() * 1000.0, 0.0)
        spread_ticks = quote.spread / tick_size if tick_size > 0 else 0.0
        trade_window_seconds = max(self.config.strategy.trade_window_ms / 1000.0, 1e-9)
        trade_rate = len(state.recent_trades) / trade_window_seconds
        top_depth = min(quote.bid_size, quote.ask_size)
        recent_prices = [trade.price for trade in state.recent_trades]
        jump_ticks = 0.0
        if recent_prices and tick_size > 0:
            jump_ticks = (max(recent_prices) - min(recent_prices)) / tick_size

        market_ok = True
        abnormal = False
        reasons: list[str] = []

        max_spread_ticks = symbol_config.max_spread_ticks or self.config.strategy.max_spread_ticks
        min_top_depth = symbol_config.min_top_depth or self.config.strategy.min_top_depth
        min_trade_rate = symbol_config.min_trade_rate or self.config.strategy.min_trade_rate

        if spread_ticks > max_spread_ticks:
            market_ok = False
            reasons.append("spread_too_wide")
        if top_depth < min_top_depth:
            market_ok = False
            reasons.append("top_depth_too_thin")
        if trade_rate < min_trade_rate:
            market_ok = False
            reasons.append("trade_rate_too_low")
        if quote_age_ms > self.config.strategy.max_quote_age_ms:
            market_ok = False
            abnormal = True
            reasons.append("quote_stale")
        if jump_ticks > self.config.strategy.volatility_guard_ticks:
            market_ok = False
            abnormal = True
            reasons.append("jump_too_large")
        if quote.bid_price <= 0 or quote.ask_price <= 0 or quote.bid_price >= quote.ask_price:
            market_ok = False
            abnormal = True
            reasons.append("invalid_quote")

        linkage_score = self._linkage_score(state.symbol)
        overheat_long_ok, overheat_short_ok = self._overheat_filter(state, tick_size)
        short_inventory_ok = self._short_inventory_ok(
            quantity=self.config.symbol_config(state.symbol).max_shares,
            shortable_tier=state.shortable_tier,
            shortable_shares=state.shortable_shares,
        )
        return SignalFilterState(
            market_ok=market_ok,
            linkage_score=linkage_score,
            overheat_long_ok=overheat_long_ok,
            overheat_short_ok=overheat_short_ok,
            quote_age_ms=quote_age_ms,
            trade_rate_per_sec=trade_rate,
            spread_ticks=spread_ticks,
            depth_available=state.capabilities().depth_available,
            short_inventory_ok=short_inventory_ok,
            abnormal=abnormal,
            reasons=tuple(reasons),
        )

    def _linkage_score(self, symbol: str) -> float:
        references = self.config.symbol_config(symbol).reference_symbols
        if not references:
            return 0.0
        scores: list[float] = []
        for reference in references:
            snapshot = self.latest_signals.get(reference)
            if snapshot is None:
                continue
            scores.append((snapshot.long_score - snapshot.short_score) / 2.0)
        if not scores:
            return 0.0
        average = fmean(scores)
        dispersion = pstdev(scores) if len(scores) > 1 else abs(average) or 1.0
        return average / max(dispersion, 1.0)

    def _overheat_filter(self, state: SymbolMarketState, tick_size: float) -> tuple[bool, bool]:
        if not state.recent_trades or tick_size <= 0:
            return True, True
        prices = [trade.price for trade in state.recent_trades]
        long_progress_ticks = (prices[-1] - min(prices)) / tick_size
        short_progress_ticks = (max(prices) - prices[-1]) / tick_size
        return (
            long_progress_ticks <= self.config.strategy.max_price_progress_ticks,
            short_progress_ticks <= self.config.strategy.max_price_progress_ticks,
        )

    def _short_inventory_ok(self, quantity: int, shortable_tier: float | None, shortable_shares: int | None) -> bool:
        if shortable_tier is not None and shortable_tier > self.config.risk.min_shortable_tier:
            return True
        if shortable_shares is not None and shortable_shares >= (quantity * self.config.risk.min_shortable_shares_multiple):
            return True
        return False

    def _entry_regime_candidate(
        self,
        state: SymbolMarketState,
        filters: SignalFilterState,
        long_score: float,
        short_score: float,
    ) -> EntryRegime:
        if not filters.market_ok or filters.abnormal:
            return EntryRegime.NONE
        best_score = max(long_score, short_score)
        passive_enabled = self.config.strategy.passive_entry_enabled and filters.depth_available and filters.spread_ticks == 2.0
        if passive_enabled and best_score >= self.config.strategy.passive_entry_threshold:
            return EntryRegime.PASSIVE_IMPROVEMENT
        if best_score >= self.config.strategy.confirmed_entry_threshold and filters.spread_ticks <= 2.0:
            return EntryRegime.CONFIRMED_TAKER
        return EntryRegime.NONE
