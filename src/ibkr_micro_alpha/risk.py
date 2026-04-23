from __future__ import annotations

from dataclasses import dataclass, field

from .config import EngineConfig
from .types import DecisionContext, EngineMode, IntentAction, PositionState, QuoteUpdate, RiskDecision, SessionHealth, TradeIntent


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
        session_guard = self._session_guard(intent)
        if session_guard is not None:
            return session_guard
        entry_guard = self._entry_guard(context, intent)
        if entry_guard is not None:
            return entry_guard
        short_guard = self._shortability_guard(context, intent)
        if short_guard is not None:
            return short_guard

        max_quantity = min(
            self.config.risk.max_order_quantity,
            self.config.symbol_config(intent.symbol).max_shares,
            self.config.risk.max_symbol_quantity,
        )
        if self.config.mode == EngineMode.LIVE:
            max_quantity = min(max_quantity, self.config.risk.canary_quantity)
        if max_quantity <= 0:
            return RiskDecision(allowed=False, reason="zero_risk_budget", max_quantity=0)
        if intent.quantity > max_quantity:
            return RiskDecision(allowed=True, reason="clamped_quantity", max_quantity=max_quantity)
        return RiskDecision(allowed=True, reason="ok", max_quantity=intent.quantity)

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

    def _session_guard(self, intent: TradeIntent) -> RiskDecision | None:
        if self.strategy_daily_pnl <= -abs(self.config.risk.max_strategy_daily_loss):
            return self.trigger_kill_switch("strategy_daily_loss_limit")
        symbol_pnl = self.symbol_daily_pnl.get(intent.symbol, 0.0)
        if symbol_pnl <= -abs(self.config.risk.max_symbol_daily_loss):
            return RiskDecision(allowed=False, reason="symbol_daily_loss_limit", max_quantity=0)
        if self.consecutive_losses >= self.config.risk.max_consecutive_losses:
            return self.trigger_kill_switch("max_consecutive_losses")
        if self.open_positions_count >= self.config.risk.max_open_positions:
            return RiskDecision(allowed=False, reason="max_open_positions", max_quantity=0)
        return None

    def _entry_guard(self, context: DecisionContext, intent: TradeIntent) -> RiskDecision | None:
        if context.pending_orders > 0:
            return RiskDecision(allowed=False, reason="pending_orders_present", max_quantity=0)
        if context.signal is None:
            return RiskDecision(allowed=False, reason="missing_signal", max_quantity=0)
        if not context.signal.filters.market_ok or context.signal.filters.abnormal:
            return RiskDecision(allowed=False, reason="market_quality_block", max_quantity=0)
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
