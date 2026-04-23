from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from .config import EngineConfig
from .types import SessionRegime


def _parse_clock(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(hour=int(hour), minute=int(minute))


def market_time(ts: datetime, timezone_name: str) -> datetime:
    market_tz = ZoneInfo(timezone_name)
    if ts.tzinfo is None:
        aware = ts.replace(tzinfo=UTC)
    else:
        aware = ts.astimezone(UTC)
    return aware.astimezone(market_tz)


def classify_session(config: EngineConfig, ts: datetime) -> SessionRegime:
    local_ts = market_time(ts, config.timezone)
    if local_ts.weekday() >= 5:
        return SessionRegime.OFF
    clock = local_ts.time()
    for regime in (
        SessionRegime.PRE,
        SessionRegime.OPEN,
        SessionRegime.CORE,
        SessionRegime.CLOSE,
        SessionRegime.POST,
    ):
        regime_config = config.strategy.session_regime_for(regime)
        if not regime_config.enabled:
            continue
        start = _parse_clock(regime_config.start_time)
        end = _parse_clock(regime_config.end_time)
        if start <= clock < end:
            return regime
    return SessionRegime.OFF


def is_extended_hours(session_regime: SessionRegime) -> bool:
    return session_regime in {SessionRegime.PRE, SessionRegime.POST}
