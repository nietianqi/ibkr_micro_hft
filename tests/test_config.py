from __future__ import annotations

from pathlib import Path

from ibkr_micro_alpha.config import load_engine_config
from ibkr_micro_alpha.types import EngineMode


def test_load_engine_config_reads_l1_first_defaults() -> None:
    config = load_engine_config(Path("configs") / "default.toml", mode=EngineMode.SHADOW)
    assert config.mode == EngineMode.SHADOW
    assert config.name == "IBKR Research-Enhanced Microstructure Bi-Directional"
    assert config.default_symbols == ["AAPL", "NVDA", "AMD", "MSFT", "AMZN", "META", "SPY", "QQQ"]
    assert config.ibkr.generic_ticks == "236,233,375"
    assert config.strategy.passive_entry_enabled is True
    assert config.strategy.entry_regime_defaults.passive_entry_ttl_ms == 250
    assert config.strategy.aggressive_entry_enabled is True
    assert config.strategy.aggressive_entry_threshold == 3.25
    assert config.strategy.reservation_bias_threshold_ticks == 0.10
    assert config.strategy.weights.quote_ofi == 0.20
    assert config.risk.queue_size_scale == 0.75
    assert config.risk.min_shortable_tier == 2.5
    assert config.symbol_config("QQQ").is_etf is True
    assert config.symbol_config("QQQ").tier.value == "tier_a"
    assert config.symbol_config("AMD").allow_extended_hours is False
    assert config.strategy.session_regime_for("pre").confirmed_entry_threshold == 2.50
    assert config.risk.session_cap_for("post").size_scale == 0.40
