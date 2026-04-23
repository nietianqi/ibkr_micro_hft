from __future__ import annotations

from pathlib import Path

from ibkr_micro_alpha.config import load_engine_config
from ibkr_micro_alpha.types import EngineMode


def test_load_engine_config_reads_l1_first_defaults() -> None:
    config = load_engine_config(Path("configs") / "default.toml", mode=EngineMode.SHADOW)
    assert config.mode == EngineMode.SHADOW
    assert config.name == "IBKR L1-First Microstructure Confirmed Hybrid Scalp"
    assert config.default_symbols == ["AAPL", "NVDA", "AMD", "MSFT", "AMZN", "META", "SPY", "QQQ"]
    assert config.ibkr.generic_ticks == "236,233,375"
    assert config.strategy.passive_entry_enabled is True
    assert config.strategy.entry_regime_defaults.passive_entry_ttl_ms == 250
    assert config.strategy.weights.quote_ofi == 0.20
    assert config.risk.min_shortable_tier == 2.5
    assert config.symbol_config("QQQ").is_etf is True
