from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib

from .types import EngineMode


@dataclass(slots=True)
class SymbolConfig:
    tick_size: float = 0.01
    max_shares: int = 50
    reference_symbols: list[str] = field(default_factory=list)
    min_top_depth: float = 100.0
    min_trade_rate: float = 5.0
    max_spread_ticks: float = 2.0
    is_etf: bool = False


@dataclass(slots=True)
class RuntimeConfig:
    shutdown_grace_seconds: int = 10
    heartbeat_seconds: int = 5
    event_queue_size: int = 10000
    capture_signals_in_capture_mode: bool = False
    reconcile_interval_seconds: int = 30


@dataclass(slots=True)
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 101
    account: str = ""
    read_only: bool = False
    market_data_type: int = 1
    depth_levels: int = 5
    reconnect_delay_seconds: float = 5.0
    snapshot_permissions_only: bool = False
    generic_ticks: str = "236,233,375"
    subscribe_depth: bool = True


@dataclass(slots=True)
class StrategyWeights:
    l1_imbalance: float = 0.15
    quote_ofi: float = 0.20
    tape_ofi: float = 0.20
    trade_burst: float = 0.15
    microprice_tilt: float = 0.15
    microprice_momentum: float = 0.15
    linkage: float = 0.25
    depth_bonus: float = 0.30


@dataclass(slots=True)
class EntryRegimeDefaults:
    confirmed_taker_threshold: float = 2.25
    passive_improvement_threshold: float = 1.75
    passive_entry_ttl_ms: int = 250
    passive_entry_max_retries: int = 1


@dataclass(slots=True)
class StrategyConfig:
    signal_window: int = 240
    trade_window_ms: int = 1000
    quote_window_ms: int = 1000
    depth_window_ms: int = 1000
    trade_burst_window_ms: int = 500
    microprice_window_ms: int = 800
    confirmed_min_signal_agree: int = 4
    confirmed_entry_threshold: float = 2.25
    passive_entry_threshold: float = 1.75
    score_collapse_threshold: float = 0.50
    soft_hold_ms: int = 3000
    soft_hold_score_threshold: float = 0.75
    hard_hold_ms: int = 12000
    max_price_progress_ticks: float = 1.0
    max_payup_ticks: float = 1.0
    tp_ticks: float = 1.0
    strong_tp_ticks: float = 2.0
    min_trade_rate: float = 5.0
    min_top_depth: float = 100.0
    max_spread_ticks: float = 2.0
    max_quote_age_ms: int = 1500
    volatility_guard_ticks: float = 8.0
    passive_entry_enabled: bool = True
    depth_bonus_enabled: bool = True
    weights: StrategyWeights = field(default_factory=StrategyWeights)
    entry_regime_defaults: EntryRegimeDefaults = field(default_factory=EntryRegimeDefaults)

    # Compatibility fields kept for existing code paths and configs.
    min_signal_agree: int = 4
    long_entry_threshold: float = 2.25
    short_entry_threshold: float = 2.25
    exit_score_threshold: float = 0.50
    max_hold_seconds: int = 12
    max_chase_ticks: float = 1.0


@dataclass(slots=True)
class RiskConfig:
    max_order_quantity: int = 50
    max_symbol_quantity: int = 100
    max_open_positions: int = 3
    max_symbol_daily_loss: float = 150.0
    max_strategy_daily_loss: float = 400.0
    max_consecutive_losses: int = 4
    max_spread_kill_ticks: float = 4.0
    stale_quote_kill_ms: int = 3000
    canary_quantity: int = 1
    per_trade_risk_dollars: float = 15.0
    vol_floor_cents: float = 0.03
    depth_participation_rate: float = 0.1
    min_shortable_tier: float = 2.5
    min_shortable_shares_multiple: int = 5


@dataclass(slots=True)
class StorageConfig:
    root_dir: str = "runtime"
    sqlite_path: str = "runtime/state/engine.sqlite3"
    parquet_root: str = "runtime/parquet"
    flush_rows: int = 200


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    json: bool = True


@dataclass(slots=True)
class EngineConfig:
    name: str = "IBKR L1-First Microstructure Confirmed Hybrid Scalp"
    timezone: str = "America/New_York"
    default_symbols: list[str] = field(default_factory=lambda: ["AAPL", "NVDA", "AMD", "MSFT", "AMZN", "META", "SPY", "QQQ"])
    mode: EngineMode = EngineMode.SHADOW
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    symbols: dict[str, SymbolConfig] = field(default_factory=dict)

    def symbol_config(self, symbol: str) -> SymbolConfig:
        return self.symbols.get(symbol, SymbolConfig())


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _merge_dataclass(instance: Any, payload: dict[str, Any]) -> Any:
    for key, value in payload.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def load_engine_config(path: str | Path, mode: EngineMode | None = None) -> EngineConfig:
    config_path = Path(path)
    payload = _load_toml(config_path)
    config = EngineConfig()
    top_level = {
        key: value
        for key, value in payload.items()
        if key not in {"runtime", "ibkr", "strategy", "risk", "storage", "logging", "symbols"}
    }
    _merge_dataclass(config, top_level)
    _merge_dataclass(config.runtime, payload.get("runtime", {}))
    _merge_dataclass(config.ibkr, payload.get("ibkr", {}))
    strategy_payload = dict(payload.get("strategy", {}))
    weights_payload = strategy_payload.pop("weights", {})
    regime_payload = strategy_payload.pop("entry_regime_defaults", {})
    _merge_dataclass(config.strategy, strategy_payload)
    _merge_dataclass(config.strategy.weights, weights_payload)
    _merge_dataclass(config.strategy.entry_regime_defaults, regime_payload)
    _merge_dataclass(config.risk, payload.get("risk", {}))
    _merge_dataclass(config.storage, payload.get("storage", {}))
    _merge_dataclass(config.logging, payload.get("logging", {}))
    for symbol, symbol_payload in payload.get("symbols", {}).items():
        symbol_config = SymbolConfig()
        _merge_dataclass(symbol_config, symbol_payload)
        config.symbols[symbol] = symbol_config
    if mode is not None:
        config.mode = mode
    elif "mode" in payload:
        config.mode = EngineMode(payload["mode"])
    return config
