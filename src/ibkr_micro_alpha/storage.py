from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
import json
import sqlite3
from typing import Any

from .config import EngineConfig
from .types import FillEvent, OrderState, PositionState, RiskDecision, SessionHealth, SignalSnapshot, TradeIntent


def _serialize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _serialize(raw) for key, raw in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _serialize(raw) for key, raw in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(raw) for raw in value]
    return value


class SQLiteStore:
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.db_path = Path(config.storage.sqlite_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        cursor = self.connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                local_order_id TEXT PRIMARY KEY,
                broker_order_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                filled_quantity INTEGER NOT NULL,
                remaining_quantity INTEGER NOT NULL,
                limit_price REAL,
                take_profit_price REAL,
                parent_local_order_id TEXT,
                purpose TEXT,
                last_fill_price REAL,
                reason TEXT,
                submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_order_id TEXT NOT NULL,
                broker_order_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                fill_price REAL NOT NULL,
                fill_size INTEGER NOT NULL,
                ts_event TEXT NOT NULL,
                ts_local TEXT NOT NULL,
                commission REAL NOT NULL,
                liquidity TEXT,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                avg_price REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                opened_at TEXT,
                updated_at TEXT NOT NULL,
                entry_order_id TEXT,
                take_profit_order_id TEXT
            );

            CREATE TABLE IF NOT EXISTS risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT,
                allowed INTEGER NOT NULL,
                kill_switch INTEGER NOT NULL,
                reason TEXT NOT NULL,
                max_quantity INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_summaries (
                session_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                connected INTEGER NOT NULL,
                reconnect_count INTEGER NOT NULL,
                kill_switch_engaged INTEGER NOT NULL,
                warnings_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def record_order(self, order: OrderState) -> None:
        self.connection.execute(
            """
            INSERT INTO orders (
                local_order_id, broker_order_id, symbol, side, status, quantity, filled_quantity, remaining_quantity,
                limit_price, take_profit_price, parent_local_order_id, purpose, last_fill_price, reason, submitted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(local_order_id) DO UPDATE SET
                broker_order_id=excluded.broker_order_id,
                status=excluded.status,
                filled_quantity=excluded.filled_quantity,
                remaining_quantity=excluded.remaining_quantity,
                last_fill_price=excluded.last_fill_price,
                updated_at=excluded.updated_at,
                reason=excluded.reason
            """,
            (
                order.local_order_id,
                order.broker_order_id,
                order.symbol,
                order.side.value,
                order.status.value,
                order.quantity,
                order.filled_quantity,
                order.remaining_quantity,
                order.limit_price,
                order.take_profit_price,
                order.parent_local_order_id,
                order.purpose,
                order.last_fill_price,
                order.reason,
                order.submitted_at.isoformat(),
                order.updated_at.isoformat(),
            ),
        )
        self.connection.commit()

    def record_fill(self, fill: FillEvent) -> None:
        self.connection.execute(
            """
            INSERT INTO fills (
                local_order_id, broker_order_id, symbol, side, fill_price, fill_size,
                ts_event, ts_local, commission, liquidity, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.local_order_id,
                fill.broker_order_id,
                fill.symbol,
                fill.side.value,
                fill.fill_price,
                fill.fill_size,
                fill.ts_event.isoformat(),
                fill.ts_local.isoformat(),
                fill.commission,
                fill.liquidity,
                json.dumps(_serialize(fill.metadata)),
            ),
        )
        self.connection.commit()

    def record_position(self, position: PositionState) -> None:
        self.connection.execute(
            """
            INSERT INTO positions (
                symbol, side, quantity, avg_price, realized_pnl, unrealized_pnl, opened_at, updated_at, entry_order_id, take_profit_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side,
                quantity=excluded.quantity,
                avg_price=excluded.avg_price,
                realized_pnl=excluded.realized_pnl,
                unrealized_pnl=excluded.unrealized_pnl,
                opened_at=excluded.opened_at,
                updated_at=excluded.updated_at,
                entry_order_id=excluded.entry_order_id,
                take_profit_order_id=excluded.take_profit_order_id
            """,
            (
                position.symbol,
                position.side.value,
                position.quantity,
                position.avg_price,
                position.realized_pnl,
                position.unrealized_pnl,
                position.opened_at.isoformat() if position.opened_at else None,
                position.updated_at.isoformat(),
                position.entry_order_id,
                position.take_profit_order_id,
            ),
        )
        self.connection.commit()

    def record_risk(self, symbol: str | None, decision: RiskDecision) -> None:
        self.connection.execute(
            """
            INSERT INTO risk_events (created_at, symbol, allowed, kill_switch, reason, max_quantity, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).replace(tzinfo=None).isoformat(),
                symbol,
                1 if decision.allowed else 0,
                1 if decision.kill_switch else 0,
                decision.reason,
                decision.max_quantity,
                json.dumps(_serialize(decision.metadata)),
            ),
        )
        self.connection.commit()

    def record_session_summary(self, session_id: str, started_at: datetime, ended_at: datetime, health: SessionHealth, metrics: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO session_summaries (
                session_id, started_at, ended_at, mode, connected, reconnect_count, kill_switch_engaged, warnings_json, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                started_at.isoformat(),
                ended_at.isoformat(),
                health.mode.value,
                1 if health.connected else 0,
                health.reconnect_count,
                1 if health.kill_switch_engaged else 0,
                json.dumps(list(health.warnings)),
                json.dumps(_serialize(metrics)),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class ParquetAuditWriter:
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.root = Path(config.storage.parquet_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.flush_rows = config.storage.flush_rows
        self.buffers: dict[str, list[dict[str, Any]]] = {"events": [], "signals": [], "decisions": []}
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ModuleNotFoundError as exc:
            raise RuntimeError("pyarrow is required for parquet audit output") from exc
        self._pa = pa
        self._pq = pq

    def write_event(self, event: Any) -> None:
        self._append("events", event)

    def write_signal(self, signal: SignalSnapshot) -> None:
        self._append("signals", signal)

    def write_decision(self, decision: TradeIntent | RiskDecision) -> None:
        self._append("decisions", decision)

    def _append(self, bucket: str, value: Any) -> None:
        record = _serialize(value)
        record["record_type"] = bucket[:-1]
        record["written_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
        self.buffers[bucket].append(record)
        if len(self.buffers[bucket]) >= self.flush_rows:
            self.flush(bucket)

    def flush(self, bucket: str | None = None) -> None:
        if bucket is None:
            for current in list(self.buffers.keys()):
                self.flush(current)
            return
        records = self.buffers[bucket]
        if not records:
            return
        table = self._pa.Table.from_pylist(records)
        timestamp = datetime.now(UTC).replace(tzinfo=None)
        date_partition = timestamp.strftime("%Y-%m-%d")
        target_dir = self.root / bucket / f"date={date_partition}"
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"part-{timestamp.strftime('%H%M%S%f')}.parquet"
        self._pq.write_table(table, file_path)
        self.buffers[bucket] = []

    def close(self) -> None:
        self.flush()


class NoopAuditWriter:
    def write_event(self, event: Any) -> None:
        return None

    def write_signal(self, signal: SignalSnapshot) -> None:
        return None

    def write_decision(self, decision: TradeIntent | RiskDecision) -> None:
        return None

    def close(self) -> None:
        return None
