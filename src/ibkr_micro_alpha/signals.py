from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import fmean, pstdev

from .config import EngineConfig
from .session import classify_session, is_extended_hours, market_time
from .state import MarketStateStore, SymbolMarketState
from .types import (
    BookUpdate,
    EngineMode,
    ExecutionState,
    EntryRegime,
    MarketMetaUpdate,
    QueueState,
    QuoteUpdate,
    SessionRegime,
    SignalFilterState,
    SignalSnapshot,
    SymbolTier,
    TradePrint,
    TradeSide,
)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


PRIMARY_KEYS = (
    "quote_ofi",
    "tape_ofi",
    "l1_imbalance",
    "trade_burst",
    "microprice_tilt",
    "microprice_momentum",
)


def _value_with_fallback(raw_value: float, zscore: float) -> float:
    if abs(zscore) > 1e-9:
        return zscore
    return raw_value


def _tick_match(value: float, target: float, tolerance: float = 0.05) -> bool:
    return abs(value - target) <= tolerance


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

        symbol_config = self.config.symbol_config(state.symbol)
        tick_size = max(symbol_config.tick_size, 1e-9)
        trade_window_seconds = max(self.config.strategy.trade_window_ms / 1000.0, 1e-9)
        trade_rate = len(state.recent_trades) / trade_window_seconds
        top_depth = min(quote.bid_size, quote.ask_size)
        spread_ticks = quote.spread / tick_size
        session_regime = classify_session(self.config, event.ts_event)

        state.push_quote_snapshot(
            event.ts_event,
            quote.mid_price,
            spread_ticks,
            top_depth,
            self.config.strategy.higher_tf_window_ms,
        )
        state.push_trade_rate(
            event.ts_event,
            trade_rate,
            self.config.strategy.higher_tf_window_ms,
        )

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
        filters, session_trade_allowed = self._build_filters(
            state=state,
            quote=quote,
            now=event.ts_local,
            session_regime=session_regime,
        )
        higher_tf_regime_score = self._compute_higher_tf_regime(
            state=state,
            quote=quote,
            trade_rate=trade_rate,
            session_regime=session_regime,
        )
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
        session_config = self.config.strategy.session_regime_for(session_regime)
        higher_tf_adjustment = session_config.higher_tf_bias_weight * higher_tf_regime_score
        long_score = base_score + linkage_adjustment + depth_adjustment + higher_tf_adjustment
        short_score = (-base_score) - linkage_adjustment - depth_adjustment - higher_tf_adjustment
        agreement_count_long = self._agreement_count(
            raw_values={
                "quote_ofi": raw_quote_ofi,
                "tape_ofi": raw_tape_ofi,
                "l1_imbalance": raw_l1_imbalance,
                "trade_burst": raw_trade_burst,
                "microprice_tilt": raw_mp_tilt,
                "microprice_momentum": raw_mp_momentum,
            },
            zscores=zscores,
            direction=1,
        )
        agreement_count_short = self._agreement_count(
            raw_values={
                "quote_ofi": raw_quote_ofi,
                "tape_ofi": raw_tape_ofi,
                "l1_imbalance": raw_l1_imbalance,
                "trade_burst": raw_trade_burst,
                "microprice_tilt": raw_mp_tilt,
                "microprice_momentum": raw_mp_momentum,
            },
            zscores=zscores,
            direction=-1,
        )
        reservation_bias = self._reservation_bias(
            microprice_tilt=raw_mp_tilt,
            weighted_imbalance=raw_weighted_imbalance,
            lob_ofi_zscore=zscores["lob_ofi"],
        )
        execution_state = self._execution_state(
            market_ok=filters.market_ok,
            abnormal=filters.abnormal,
            queue_state=filters.queue_state,
        )
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
            agreement_count_long=agreement_count_long,
            agreement_count_short=agreement_count_short,
            linkage_score=filters.linkage_score,
            reservation_bias=reservation_bias,
            market_ok=filters.market_ok,
            abnormal=filters.abnormal,
            queue_state=filters.queue_state,
            execution_state=execution_state,
            session_regime=session_regime,
            higher_tf_regime_score=higher_tf_regime_score,
            session_trade_allowed=session_trade_allowed,
            shortable_tier=state.shortable_tier,
            shortable_shares=state.shortable_shares,
            entry_regime_candidate=self._entry_regime_candidate(
                state=state,
                filters=filters,
                long_score=long_score,
                short_score=short_score,
                agreement_count_long=agreement_count_long,
                agreement_count_short=agreement_count_short,
                reservation_bias=reservation_bias,
                execution_state=execution_state,
                trade_burst_zscore=zscores["trade_burst"],
                microprice_tilt=raw_mp_tilt,
                tape_ofi=raw_tape_ofi,
                session_regime=session_regime,
                session_trade_allowed=session_trade_allowed,
            ),
            metadata={
                "mid_price": quote.mid_price,
                "market_clock": market_time(event.ts_event, self.config.timezone).isoformat(),
                "market_data_capabilities": asdict(state.capabilities()),
                "higher_tf_regime_score": higher_tf_regime_score,
                "agreement_count_long": agreement_count_long,
                "agreement_count_short": agreement_count_short,
                "linkage_score": filters.linkage_score,
                "reservation_bias": reservation_bias,
                "queue_state": filters.queue_state.value,
                "execution_state": execution_state.value,
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

    def _build_filters(
        self,
        state: SymbolMarketState,
        quote: QuoteUpdate,
        now: datetime,
        session_regime: SessionRegime,
    ) -> tuple[SignalFilterState, bool]:
        symbol_config = self.config.symbol_config(state.symbol)
        session_config = self.config.strategy.session_regime_for(session_regime)
        tick_size = max(symbol_config.tick_size, 1e-9)
        quote_age_ms = max((now - quote.ts_event).total_seconds() * 1000.0, 0.0)
        spread_ticks = quote.spread / tick_size
        trade_window_seconds = max(self.config.strategy.trade_window_ms / 1000.0, 1e-9)
        trade_rate = len(state.recent_trades) / trade_window_seconds
        top_depth = min(quote.bid_size, quote.ask_size)
        recent_prices = [trade.price for trade in state.recent_trades]
        jump_ticks = 0.0
        if recent_prices:
            jump_ticks = (max(recent_prices) - min(recent_prices)) / tick_size

        market_ok = True
        abnormal = False
        reasons: list[str] = []
        session_reasons: list[str] = []

        max_spread_ticks = (
            session_config.max_spread_ticks
            if session_config.max_spread_ticks is not None
            else (
                symbol_config.max_spread_ticks
                if symbol_config.max_spread_ticks is not None
                else self.config.strategy.max_spread_ticks
            )
        )
        min_top_depth = (
            session_config.min_top_depth
            if session_config.min_top_depth is not None
            else (
                symbol_config.min_top_depth
                if symbol_config.min_top_depth is not None
                else self.config.strategy.min_top_depth
            )
        )
        min_trade_rate = (
            session_config.min_trade_rate
            if session_config.min_trade_rate is not None
            else (
                symbol_config.min_trade_rate
                if symbol_config.min_trade_rate is not None
                else self.config.strategy.min_trade_rate
            )
        )
        max_progress_ticks = (
            session_config.max_price_progress_ticks
            if session_config.max_price_progress_ticks is not None
            else self.config.strategy.max_price_progress_ticks
        )
        queue_thin_depth = max(min_top_depth, min_top_depth * self.config.strategy.queue_thin_depth_ratio)
        queue_state = QueueState.THIN if _tick_match(spread_ticks, 1.0) or top_depth < queue_thin_depth else QueueState.NORMAL

        session_trade_allowed = True
        if session_regime == SessionRegime.OFF:
            session_trade_allowed = False
            session_reasons.append("session_off")
        if not session_config.enabled:
            session_trade_allowed = False
            session_reasons.append("session_disabled")
        if not session_config.allow_entry:
            session_trade_allowed = False
            session_reasons.append("session_entry_disabled")
        if symbol_config.tier == SymbolTier.WATCHLIST:
            session_trade_allowed = False
            session_reasons.append("watchlist_only")
        if (
            self.config.mode == EngineMode.LIVE
            and is_extended_hours(session_regime)
            and not symbol_config.allow_extended_hours
        ):
            session_trade_allowed = False
            session_reasons.append("extended_hours_live_disabled")

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
        if not session_trade_allowed:
            market_ok = False

        linkage_score = self._linkage_score(state.symbol)
        overheat_long_ok, overheat_short_ok = self._overheat_filter(
            state=state,
            tick_size=tick_size,
            max_progress_ticks=max_progress_ticks,
        )
        short_inventory_ok = self._short_inventory_ok(
            quantity=self.config.symbol_config(state.symbol).max_shares,
            shortable_tier=state.shortable_tier,
            shortable_shares=state.shortable_shares,
        )
        return (
            SignalFilterState(
                market_ok=market_ok,
                linkage_score=linkage_score,
                overheat_long_ok=overheat_long_ok,
                overheat_short_ok=overheat_short_ok,
                quote_age_ms=quote_age_ms,
                trade_rate_per_sec=trade_rate,
                spread_ticks=spread_ticks,
                depth_available=state.capabilities().depth_available,
                short_inventory_ok=short_inventory_ok,
                queue_state=queue_state,
                abnormal=abnormal,
                reasons=tuple(reasons),
                session_reasons=tuple(session_reasons),
            ),
            session_trade_allowed,
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

    def _agreement_count(self, raw_values: dict[str, float], zscores: dict[str, float], direction: int) -> int:
        values = {
            key: _value_with_fallback(raw_values[key], zscores.get(key, 0.0))
            for key in PRIMARY_KEYS
        }
        if direction > 0:
            return sum(1 for key in PRIMARY_KEYS if values[key] > 0)
        return sum(1 for key in PRIMARY_KEYS if values[key] < 0)

    def _reservation_bias(self, microprice_tilt: float, weighted_imbalance: float, lob_ofi_zscore: float) -> float:
        return microprice_tilt + (0.35 * weighted_imbalance) + (0.15 * _clamp(lob_ofi_zscore, -2.0, 2.0))

    def _execution_state(
        self,
        market_ok: bool,
        abnormal: bool,
        queue_state: QueueState,
    ) -> ExecutionState:
        if abnormal:
            return ExecutionState.ABNORMAL
        if queue_state == QueueState.THIN and market_ok:
            return ExecutionState.QUEUE
        return ExecutionState.NORMAL

    def _overheat_filter(
        self,
        state: SymbolMarketState,
        tick_size: float,
        max_progress_ticks: float,
    ) -> tuple[bool, bool]:
        if not state.recent_trades or tick_size <= 0:
            return True, True
        prices = [trade.price for trade in state.recent_trades]
        long_progress_ticks = (prices[-1] - min(prices)) / tick_size
        short_progress_ticks = (max(prices) - prices[-1]) / tick_size
        return (
            long_progress_ticks <= max_progress_ticks,
            short_progress_ticks <= max_progress_ticks,
        )

    def _short_inventory_ok(self, quantity: int, shortable_tier: float | None, shortable_shares: int | None) -> bool:
        if shortable_tier is not None and shortable_tier > self.config.risk.min_shortable_tier:
            return True
        if shortable_shares is not None and shortable_shares >= (quantity * self.config.risk.min_shortable_shares_multiple):
            return True
        return False

    def _window_baseline(
        self,
        series: list[tuple[datetime, float, float, float]],
        cutoff: datetime,
    ) -> tuple[datetime, float, float, float] | None:
        if not series:
            return None
        for item in series:
            if item[0] >= cutoff:
                return item
        return series[0]

    def _compute_higher_tf_regime(
        self,
        state: SymbolMarketState,
        quote: QuoteUpdate,
        trade_rate: float,
        session_regime: SessionRegime,
    ) -> float:
        tick_size = max(self.config.symbol_config(state.symbol).tick_size, 1e-9)
        if not state.quote_history:
            return 0.0
        history = list(state.quote_history)
        now = quote.ts_event
        current_mid = quote.mid_price
        current_spread = quote.spread / tick_size
        current_depth = min(quote.bid_size, quote.ask_size)

        def return_ticks(window_minutes: int) -> float:
            baseline = self._window_baseline(history, now - timedelta(minutes=window_minutes))
            if baseline is None:
                return 0.0
            return (current_mid - baseline[1]) / tick_size

        ret_1m = return_ticks(1)
        ret_5m = return_ticks(5)
        ret_15m = return_ticks(15)
        directional_score = (
            0.45 * _clamp(ret_1m / 6.0, -1.0, 1.0)
            + 0.35 * _clamp(ret_5m / 12.0, -1.0, 1.0)
            + 0.20 * _clamp(ret_15m / 20.0, -1.0, 1.0)
        )

        spreads = [item[2] for item in history]
        depths = [item[3] for item in history]
        mean_spread = fmean(spreads) if spreads else current_spread
        mean_depth = fmean(depths) if depths else current_depth
        recent_trade_rates = [value for _, value in state.trade_rate_history]
        mean_trade_rate = fmean(recent_trade_rates) if recent_trade_rates else max(trade_rate, 1.0)
        spread_regime = _clamp((mean_spread - current_spread) / max(mean_spread, 1.0), -1.0, 1.0)
        depth_regime = _clamp((current_depth - mean_depth) / max(mean_depth, 1.0), -1.0, 1.0)
        trade_regime = _clamp((trade_rate - mean_trade_rate) / max(mean_trade_rate, 1.0), -1.0, 1.0)

        recent_1m_points = [item[1] for item in history if item[0] >= now - timedelta(minutes=1)]
        if len(recent_1m_points) >= 2:
            realized_range_ticks = (max(recent_1m_points) - min(recent_1m_points)) / tick_size
        else:
            realized_range_ticks = 0.0
        volatility_penalty = _clamp(
            (realized_range_ticks - (self.config.strategy.volatility_guard_ticks * 0.5))
            / max(self.config.strategy.volatility_guard_ticks, 1.0),
            0.0,
            1.0,
        )

        reference_scores: list[float] = []
        for reference in self.config.symbol_config(state.symbol).reference_symbols:
            reference_snapshot = self.latest_signals.get(reference)
            if reference_snapshot is None:
                continue
            reference_scores.append(reference_snapshot.higher_tf_regime_score)
        reference_regime = fmean(reference_scores) if reference_scores else 0.0

        session_bias = 0.0
        if session_regime in {SessionRegime.PRE, SessionRegime.POST}:
            session_bias = 0.10 * directional_score

        score = (
            directional_score
            + (0.15 * spread_regime)
            + (0.15 * depth_regime)
            + (0.10 * trade_regime)
            + (0.20 * _clamp(reference_regime / 2.0, -1.0, 1.0))
            + session_bias
            - (0.20 * volatility_penalty)
        )
        return _clamp(score, -3.0, 3.0)

    def _entry_regime_candidate(
        self,
        state: SymbolMarketState,
        filters: SignalFilterState,
        long_score: float,
        short_score: float,
        agreement_count_long: int,
        agreement_count_short: int,
        reservation_bias: float,
        execution_state: ExecutionState,
        trade_burst_zscore: float,
        microprice_tilt: float,
        tape_ofi: float,
        session_regime: SessionRegime,
        session_trade_allowed: bool,
    ) -> EntryRegime:
        if not session_trade_allowed or not filters.market_ok or filters.abnormal:
            return EntryRegime.NONE
        session_config = self.config.strategy.session_regime_for(session_regime)
        queue_bonus = self.config.strategy.queue_entry_threshold_bonus if execution_state == ExecutionState.QUEUE else 0.0
        queue_agreement_bonus = (
            self.config.strategy.queue_min_signal_agree_bonus
            if execution_state == ExecutionState.QUEUE
            else 0
        )
        long_confirm_threshold = session_config.confirmed_entry_threshold + queue_bonus
        short_confirm_threshold = session_config.confirmed_entry_threshold + queue_bonus
        min_agree = session_config.confirmed_min_signal_agree + queue_agreement_bonus
        long_best = long_score >= short_score
        best_score = long_score if long_best else short_score
        best_agreement = agreement_count_long if long_best else agreement_count_short
        best_reservation_bias = reservation_bias if long_best else -reservation_bias
        best_tape_ofi = tape_ofi if long_best else -tape_ofi
        best_microprice_tilt = microprice_tilt if long_best else -microprice_tilt

        aggressive_enabled = (
            self.config.strategy.aggressive_entry_enabled
            and execution_state == ExecutionState.NORMAL
            and filters.spread_ticks <= self.config.strategy.aggressive_max_spread_ticks
            and best_score >= self.config.strategy.aggressive_entry_threshold
            and best_agreement >= self.config.strategy.aggressive_min_signal_agree
            and trade_burst_zscore >= self.config.strategy.aggressive_trade_burst_zscore
            and best_tape_ofi > 0
            and best_microprice_tilt >= self.config.strategy.reservation_bias_threshold_ticks
        )
        if aggressive_enabled:
            return EntryRegime.AGGRESSIVE_TAKER

        passive_enabled = (
            self.config.strategy.passive_entry_enabled
            and session_config.allow_passive_entry
            and filters.depth_available
            and _tick_match(filters.spread_ticks, 2.0)
            and execution_state == ExecutionState.NORMAL
        )
        if (
            passive_enabled
            and best_score >= session_config.passive_entry_threshold
            and best_agreement >= session_config.confirmed_min_signal_agree
            and best_reservation_bias >= self.config.strategy.reservation_bias_threshold_ticks
        ):
            return EntryRegime.PASSIVE_IMPROVEMENT
        if (
            best_score >= max(long_confirm_threshold, short_confirm_threshold)
            and best_agreement >= min_agree
            and filters.spread_ticks <= 2.0
        ):
            return EntryRegime.CONFIRMED_TAKER
        return EntryRegime.NONE
