from __future__ import annotations

from dataclasses import dataclass, field

from .config import EngineConfig
from .types import DecisionContext, EngineMode, ExecutionState, IntentAction, PositionState, QuoteUpdate, RiskDecision, SessionHealth, SymbolTier, TradeIntent


@dataclass(slots=True)
class HardRiskManager:
    config: EngineConfig
    symbol_daily_pnl: dict[str, float] = field(default_factory=dict)
    strategy_daily_pnl: float = 0.0
    consecutive_losses: int = 0
    kill_switch_reason: str | None = None
    kill_switch_metadata: dict[str, str] = field(default_factory=dict)
    open_positions_count: int = 0

    @property
    def kill_switch_engaged(self) -> bool:
        return self.kill_switch_reason is not None

    def evaluate(self, context: DecisionContext, intent: TradeIntent) -> RiskDecision:
        if intent.action == IntentAction.NOOP:
            return RiskDecision(allowed=False, reason="noop", max_quantity=0)
        if intent.action in {IntentAction.EXIT, IntentAction.FLATTEN, IntentAction.CANCEL, IntentAction.PLACE_TAKE_PROFIT}:
            return RiskDecision(allowed=True, reason="risk_reducing", max_quantity=max(intent.quantity, 0))

        if self.kill_switch_engaged:
            return RiskDecision(
                allowed=False,
                reason=self.kill_switch_reason or "kill_switch",
                max_quantity=0,
                kill_switch=True,
                metadata=dict(self.kill_switch_metadata),
            )
        if context.quote is None:
            return RiskDecision(allowed=False, reason="missing_quote", max_quantity=0)

        health_check = self._health_guard(context.session_health, context.quote)
        if health_check is not None:
            return health_check
        session_guard = self._session_guard(context, intent)
        if session_guard is not None:
            return session_guard
        session_loss_guard = self._session_loss_guard(intent)
        if session_loss_guard is not None:
            return session_loss_guard
        entry_guard = self._entry_guard(context, intent)
        if entry_guard is not None:
            return entry_guard
        short_guard = self._shortability_guard(context, intent)
        if short_guard is not None:
            return short_guard

        max_quantity, metadata = self._max_entry_quantity(context, intent)
        if self.config.mode == EngineMode.LIVE:
            max_quantity = min(max_quantity, self.config.risk.canary_quantity)
            metadata["live_canary_quantity"] = self.config.risk.canary_quantity
        if max_quantity <= 0:
            return RiskDecision(allowed=False, reason="zero_risk_budget", max_quantity=0, metadata=metadata)
        if intent.quantity > max_quantity:
            return RiskDecision(allowed=True, reason="clamped_quantity", max_quantity=max_quantity, metadata=metadata)
        return RiskDecision(allowed=True, reason="ok", max_quantity=intent.quantity, metadata=metadata)

    def _health_guard(self, session_health: SessionHealth, quote: QuoteUpdate) -> RiskDecision | None:
        tick_size = self.config.symbol_config(quote.symbol).tick_size
        spread_ticks = quote.spread / tick_size if tick_size > 0 else 0.0
        if not session_health.connected:
            return self.trigger_kill_switch("connection_lost")
        if session_health.data_stale:
            return self.trigger_kill_switch("data_stale")
        if spread_ticks > self.config.risk.max_spread_kill_ticks:
            return self.trigger_kill_switch("spread_guard")
        if session_health.kill_switch_engaged:
            return self.trigger_kill_switch(session_health.kill_switch_reason or "kill_switch")
        return None

    def _session_guard(self, context: DecisionContext, intent: TradeIntent) -> RiskDecision | None:
        session_cap = self.config.risk.session_cap_for(context.session_regime)
        symbol_config = self.config.symbol_config(intent.symbol)

        if symbol_config.tier == SymbolTier.WATCHLIST:
            return RiskDecision(allowed=False, reason="watchlist_only", max_quantity=0)
        if not session_cap.allow_new_entries:
            return RiskDecision(allowed=False, reason="session_entry_disabled", max_quantity=0)
        if intent.action == IntentAction.OPEN_LONG and not session_cap.allow_long:
            return RiskDecision(allowed=False, reason="session_long_disabled", max_quantity=0)
        if intent.action == IntentAction.OPEN_SHORT and not session_cap.allow_short:
            return RiskDecision(allowed=False, reason="session_short_disabled", max_quantity=0)

        session_open_positions = session_cap.max_open_positions
        if session_open_positions is not None and self.open_positions_count >= min(session_open_positions, self.config.risk.max_open_positions):
            return RiskDecision(allowed=False, reason="session_open_positions_cap", max_quantity=0)
        if self.open_positions_count >= self.config.risk.max_open_positions:
            return RiskDecision(allowed=False, reason="max_open_positions", max_quantity=0)

        if context.extended_hours:
            if not symbol_config.allow_extended_hours:
                return RiskDecision(allowed=False, reason="extended_hours_disabled", max_quantity=0)
            if intent.action == IntentAction.OPEN_SHORT and not symbol_config.allow_short_extended:
                return RiskDecision(allowed=False, reason="extended_hours_short_disabled", max_quantity=0)
        return None

    def _session_loss_guard(self, intent: TradeIntent) -> RiskDecision | None:
        if self.strategy_daily_pnl <= -abs(self.config.risk.max_strategy_daily_loss):
            return self.trigger_kill_switch("strategy_daily_loss_limit")
        symbol_pnl = self.symbol_daily_pnl.get(intent.symbol, 0.0)
        if symbol_pnl <= -abs(self.config.risk.max_symbol_daily_loss):
            return RiskDecision(allowed=False, reason="symbol_daily_loss_limit", max_quantity=0)
        if self.consecutive_losses >= self.config.risk.max_consecutive_losses:
            return self.trigger_kill_switch("max_consecutive_losses")
        return None

    def _entry_guard(self, context: DecisionContext, intent: TradeIntent) -> RiskDecision | None:
        if context.pending_orders > 0:
            return RiskDecision(allowed=False, reason="pending_orders_present", max_quantity=0)
        if context.signal is None:
            return RiskDecision(allowed=False, reason="missing_signal", max_quantity=0)
        if not context.signal.session_trade_allowed:
            return RiskDecision(
                allowed=False,
                reason="session_trade_block",
                max_quantity=0,
                metadata={"session_reasons": ",".join(context.signal.filters.session_reasons)},
            )
        if context.execution_state == ExecutionState.ABNORMAL:
            return RiskDecision(
                allowed=False,
                reason="execution_state_abnormal",
                max_quantity=0,
                metadata={"execution_state": context.execution_state.value},
            )
        if not context.signal.filters.market_ok or context.signal.filters.abnormal:
            return RiskDecision(
                allowed=False,
                reason="market_quality_block",
                max_quantity=0,
                metadata={
                    "reasons": ",".join(context.signal.filters.reasons),
                    "session_reasons": ",".join(context.signal.filters.session_reasons),
                },
            )
        return None

    def _shortability_guard(self, context: DecisionContext, intent: TradeIntent) -> RiskDecision | None:
        if intent.action != IntentAction.OPEN_SHORT:
            return None
        if self.short_inventory_ok(intent.quantity, context.shortable_tier, context.shortable_shares):
            return None
        return RiskDecision(
            allowed=False,
            reason="short_inventory_block",
            max_quantity=0,
            metadata={
                "shortable_tier": str(context.shortable_tier),
                "shortable_shares": str(context.shortable_shares),
            },
        )

    def _max_entry_quantity(self, context: DecisionContext, intent: TradeIntent) -> tuple[int, dict[str, float | int | str]]:
        assert context.quote is not None
        symbol_config = self.config.symbol_config(intent.symbol)
        session_cap = self.config.risk.session_cap_for(context.session_regime)
        tick_size = max(symbol_config.tick_size, 1e-9)
        signal = context.signal

        spread_cents = max(context.quote.spread, tick_size)
        stop_cents = tick_size * max(signal.filters.spread_ticks if signal is not None else 1.0, 1.0)
        risk_per_share = max(stop_cents, spread_cents, self.config.risk.vol_floor_cents)
        size_raw = int(self.config.risk.per_trade_risk_dollars / risk_per_share) if risk_per_share > 0 else 0
        depth_cap = int(self.config.risk.depth_participation_rate * min(context.quote.bid_size, context.quote.ask_size))

        base_limit = min(
            self.config.risk.max_order_quantity,
            self.config.risk.max_symbol_quantity,
            symbol_config.max_shares,
        )
        scaled_limit = int(base_limit * session_cap.size_scale)
        aligned_scale = 1.0
        if signal is not None:
            alignment = signal.higher_tf_regime_score
            if intent.action == IntentAction.OPEN_SHORT:
                alignment = -alignment
            if alignment < 0:
                aligned_scale = 0.75
            else:
                aligned_scale = 1.0 + min(alignment, 2.0) * 0.10

        candidate = min(size_raw, depth_cap, max(scaled_limit, 0))
        final_quantity = int(candidate * aligned_scale)
        if context.execution_state == ExecutionState.QUEUE:
            final_quantity = int(final_quantity * self.config.risk.queue_size_scale)
        final_quantity = min(final_quantity, base_limit)
        metadata: dict[str, float | int | str] = {
            "session_regime": context.session_regime.value,
            "session_size_scale": session_cap.size_scale,
            "spread_cents": round(spread_cents, 6),
            "stop_cents": round(stop_cents, 6),
            "risk_per_share": round(risk_per_share, 6),
            "size_raw": size_raw,
            "depth_cap": depth_cap,
            "base_limit": base_limit,
            "scaled_limit": scaled_limit,
            "aligned_scale": round(aligned_scale, 4),
            "queue_size_scale": self.config.risk.queue_size_scale if context.execution_state == ExecutionState.QUEUE else 1.0,
            "execution_state": context.execution_state.value,
            "extended_hours": str(context.extended_hours).lower(),
        }
        return max(final_quantity, 0), metadata

    def short_inventory_ok(self, quantity: int, shortable_tier: float | None, shortable_shares: int | None) -> bool:
        if shortable_tier is not None and shortable_tier > self.config.risk.min_shortable_tier:
            return True
        if shortable_shares is not None and shortable_shares >= (quantity * self.config.risk.min_shortable_shares_multiple):
            return True
        return False

    def register_closed_position(self, position: PositionState) -> None:
        symbol_pnl = self.symbol_daily_pnl.get(position.symbol, 0.0) + position.realized_pnl
        self.symbol_daily_pnl[position.symbol] = symbol_pnl
        self.strategy_daily_pnl += position.realized_pnl
        if position.realized_pnl < 0:
            self.consecutive_losses += 1
        elif position.realized_pnl > 0:
            self.consecutive_losses = 0

    def update_open_positions(self, open_positions_count: int) -> None:
        self.open_positions_count = open_positions_count

    def trigger_kill_switch(self, reason: str, metadata: dict[str, str] | None = None) -> RiskDecision:
        self.kill_switch_reason = reason
        self.kill_switch_metadata = metadata or {}
        return RiskDecision(
            allowed=False,
            reason=reason,
            max_quantity=0,
            kill_switch=True,
            metadata=dict(self.kill_switch_metadata),
        )

    def register_reconcile_mismatch(self, details: str) -> RiskDecision:
        return self.trigger_kill_switch("reconcile_mismatch", {"details": details})

    def register_unexpected_broker_position(self, symbol: str) -> RiskDecision:
        return self.trigger_kill_switch("unexpected_broker_position", {"symbol": symbol})

    def release_kill_switch(self) -> None:
        self.kill_switch_reason = None
        self.kill_switch_metadata = {}
