from __future__ import annotations

from dataclasses import dataclass

from .config import EngineConfig
from .types import DecisionContext, EntryRegime, IntentAction, PositionSide, TradeIntent, TradeSide


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


def _signal_agreement(context: DecisionContext, direction: int) -> int:
    assert context.signal is not None
    signal = context.signal
    values = {
        "quote_ofi": _value_with_fallback(signal.quote_ofi, signal.zscores.get("quote_ofi", 0.0)),
        "tape_ofi": _value_with_fallback(signal.tape_ofi, signal.zscores.get("tape_ofi", 0.0)),
        "l1_imbalance": _value_with_fallback(signal.l1_imbalance, signal.zscores.get("l1_imbalance", 0.0)),
        "trade_burst": _value_with_fallback(signal.trade_burst, signal.zscores.get("trade_burst", 0.0)),
        "microprice_tilt": _value_with_fallback(signal.microprice_tilt, signal.zscores.get("microprice_tilt", 0.0)),
        "microprice_momentum": _value_with_fallback(
            signal.microprice_momentum,
            signal.zscores.get("microprice_momentum", 0.0),
        ),
    }
    if direction > 0:
        return sum(1 for key in PRIMARY_KEYS if values[key] > 0)
    return sum(1 for key in PRIMARY_KEYS if values[key] < 0)


@dataclass(slots=True)
class DecisionEngine:
    config: EngineConfig

    def decide(self, context: DecisionContext) -> TradeIntent | None:
        signal = context.signal
        quote = context.quote
        if signal is None or quote is None:
            return None
        if context.position and context.position.quantity:
            return self._decide_exit(context)
        if context.pending_orders > 0:
            return None
        if not signal.filters.market_ok or signal.filters.abnormal:
            return None

        tick_size = self.config.symbol_config(signal.symbol).tick_size
        quantity = self.config.symbol_config(signal.symbol).max_shares
        long_agreement = _signal_agreement(context, 1)
        short_agreement = _signal_agreement(context, -1)

        if (
            signal.long_score >= self.config.strategy.confirmed_entry_threshold
            and long_agreement >= self.config.strategy.confirmed_min_signal_agree
            and signal.filters.overheat_long_ok
            and signal.filters.spread_ticks <= 2.0
        ):
            return TradeIntent(
                action=IntentAction.OPEN_LONG,
                symbol=signal.symbol,
                side=TradeSide.BUY,
                quantity=quantity,
                limit_price=quote.ask_price + (self.config.strategy.max_payup_ticks * tick_size),
                ts_event=signal.ts_event,
                reason="confirmed_taker_long",
                entry_regime=EntryRegime.CONFIRMED_TAKER,
                max_slippage_ticks=self.config.strategy.max_payup_ticks,
                reduce_only=False,
                purpose_detail="confirmed_taker_entry",
                metadata={"tp_ticks": self._tp_ticks(signal), "agreed_signals": long_agreement},
            )
        if (
            signal.short_score >= self.config.strategy.confirmed_entry_threshold
            and short_agreement >= self.config.strategy.confirmed_min_signal_agree
            and signal.filters.overheat_short_ok
            and signal.filters.spread_ticks <= 2.0
            and context.short_inventory_ok
        ):
            return TradeIntent(
                action=IntentAction.OPEN_SHORT,
                symbol=signal.symbol,
                side=TradeSide.SELL,
                quantity=quantity,
                limit_price=quote.bid_price - (self.config.strategy.max_payup_ticks * tick_size),
                ts_event=signal.ts_event,
                reason="confirmed_taker_short",
                entry_regime=EntryRegime.CONFIRMED_TAKER,
                max_slippage_ticks=self.config.strategy.max_payup_ticks,
                reduce_only=False,
                purpose_detail="confirmed_taker_entry",
                metadata={"tp_ticks": self._tp_ticks(signal), "agreed_signals": short_agreement},
            )

        if not self.config.strategy.passive_entry_enabled or not context.passive_retry_available:
            return None
        if signal.filters.spread_ticks != 2.0 or not signal.depth_available:
            return None

        if (
            signal.long_score >= self.config.strategy.passive_entry_threshold
            and long_agreement >= self.config.strategy.confirmed_min_signal_agree
            and signal.filters.overheat_long_ok
            and signal.weighted_imbalance > 0
            and signal.lob_ofi > 0
        ):
            return TradeIntent(
                action=IntentAction.OPEN_LONG,
                symbol=signal.symbol,
                side=TradeSide.BUY,
                quantity=quantity,
                limit_price=quote.bid_price,
                ts_event=signal.ts_event,
                reason="passive_improvement_long",
                entry_regime=EntryRegime.PASSIVE_IMPROVEMENT,
                max_slippage_ticks=0.0,
                reduce_only=False,
                ttl_ms=self.config.strategy.entry_regime_defaults.passive_entry_ttl_ms,
                purpose_detail="passive_improvement_entry",
                metadata={"tp_ticks": self._tp_ticks(signal), "agreed_signals": long_agreement},
            )
        if (
            signal.short_score >= self.config.strategy.passive_entry_threshold
            and short_agreement >= self.config.strategy.confirmed_min_signal_agree
            and signal.filters.overheat_short_ok
            and context.short_inventory_ok
            and signal.weighted_imbalance < 0
            and signal.lob_ofi < 0
        ):
            return TradeIntent(
                action=IntentAction.OPEN_SHORT,
                symbol=signal.symbol,
                side=TradeSide.SELL,
                quantity=quantity,
                limit_price=quote.ask_price,
                ts_event=signal.ts_event,
                reason="passive_improvement_short",
                entry_regime=EntryRegime.PASSIVE_IMPROVEMENT,
                max_slippage_ticks=0.0,
                reduce_only=False,
                ttl_ms=self.config.strategy.entry_regime_defaults.passive_entry_ttl_ms,
                purpose_detail="passive_improvement_entry",
                metadata={"tp_ticks": self._tp_ticks(signal), "agreed_signals": short_agreement},
            )
        return None

    def _decide_exit(self, context: DecisionContext) -> TradeIntent | None:
        assert context.position is not None
        assert context.signal is not None
        assert context.quote is not None

        held_ms = 0.0
        if context.position.opened_at is not None:
            held_ms = (context.signal.ts_event - context.position.opened_at).total_seconds() * 1000.0

        if context.position.side == PositionSide.LONG:
            reason = self._exit_reason_long(context, held_ms)
            if reason is None:
                return None
            return TradeIntent(
                action=IntentAction.EXIT,
                symbol=context.position.symbol,
                side=TradeSide.SELL,
                quantity=context.position.quantity,
                limit_price=context.quote.bid_price,
                ts_event=context.signal.ts_event,
                reason=reason,
                entry_regime=context.position.entry_regime,
                reduce_only=True,
                purpose_detail="protective_exit",
            )

        if context.position.side == PositionSide.SHORT:
            reason = self._exit_reason_short(context, held_ms)
            if reason is None:
                return None
            return TradeIntent(
                action=IntentAction.EXIT,
                symbol=context.position.symbol,
                side=TradeSide.BUY,
                quantity=context.position.quantity,
                limit_price=context.quote.ask_price,
                ts_event=context.signal.ts_event,
                reason=reason,
                entry_regime=context.position.entry_regime,
                reduce_only=True,
                purpose_detail="protective_exit",
            )
        return None

    def _exit_reason_long(self, context: DecisionContext, held_ms: float) -> str | None:
        signal = context.signal
        assert signal is not None
        if signal.filters.quote_age_ms > self.config.risk.stale_quote_kill_ms:
            return "stale_quote_exit"
        if signal.filters.spread_ticks > self.config.risk.max_spread_kill_ticks:
            return "spread_blowout_exit"
        if signal.long_score <= self.config.strategy.score_collapse_threshold:
            return "score_collapse_exit"
        if signal.quote_ofi < 0 or signal.tape_ofi < 0:
            return "quote_tape_flip_exit"
        if held_ms >= self.config.strategy.hard_hold_ms:
            return "hard_hold_exit"
        if held_ms >= self.config.strategy.soft_hold_ms and signal.long_score <= self.config.strategy.soft_hold_score_threshold:
            return "soft_hold_exit"
        return None

    def _exit_reason_short(self, context: DecisionContext, held_ms: float) -> str | None:
        signal = context.signal
        assert signal is not None
        if signal.filters.quote_age_ms > self.config.risk.stale_quote_kill_ms:
            return "stale_quote_exit"
        if signal.filters.spread_ticks > self.config.risk.max_spread_kill_ticks:
            return "spread_blowout_exit"
        if signal.short_score <= self.config.strategy.score_collapse_threshold:
            return "score_collapse_exit"
        if signal.quote_ofi > 0 or signal.tape_ofi > 0:
            return "quote_tape_flip_exit"
        if held_ms >= self.config.strategy.hard_hold_ms:
            return "hard_hold_exit"
        if held_ms >= self.config.strategy.soft_hold_ms and signal.short_score <= self.config.strategy.soft_hold_score_threshold:
            return "soft_hold_exit"
        return None

    def _tp_ticks(self, signal: object) -> float:
        typed_signal = signal  # Helps keep callsites readable with dataclass-based inputs.
        assert hasattr(typed_signal, "filters")
        assert hasattr(typed_signal, "trade_burst")
        assert hasattr(typed_signal, "microprice_tilt")
        if typed_signal.filters.spread_ticks == 2.0 and abs(typed_signal.trade_burst) > 0 and abs(typed_signal.microprice_tilt) > 0:
            return self.config.strategy.strong_tp_ticks
        return self.config.strategy.tp_ticks
