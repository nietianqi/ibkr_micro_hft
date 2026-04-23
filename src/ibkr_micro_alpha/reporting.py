from __future__ import annotations

from pathlib import Path
import sqlite3

from .config import EngineConfig


def render_report(config: EngineConfig, trading_date: str | None = None) -> str:
    db_path = Path(config.storage.sqlite_path)
    if not db_path.exists():
        return "No SQLite state database found."
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    fills = connection.execute(
        """
        SELECT COUNT(*) AS fill_count,
               COALESCE(SUM(fill_size * fill_price), 0) AS gross_notional
        FROM fills
        WHERE (? IS NULL OR substr(ts_event, 1, 10) = ?)
        """,
        (trading_date, trading_date),
    ).fetchone()
    risk_events = connection.execute(
        """
        SELECT COUNT(*) AS risk_events,
               COALESCE(SUM(kill_switch), 0) AS kill_switch_hits
        FROM risk_events
        WHERE (? IS NULL OR substr(created_at, 1, 10) = ?)
        """,
        (trading_date, trading_date),
    ).fetchone()
    positions = connection.execute(
        """
        SELECT symbol, side, quantity, avg_price, realized_pnl, unrealized_pnl
        FROM positions
        ORDER BY symbol
        """
    ).fetchall()
    lines = [
        "IBKR Micro Alpha Report",
        f"date={trading_date or 'all'}",
        f"fills={fills['fill_count']}",
        f"gross_notional={fills['gross_notional']:.2f}",
        f"risk_events={risk_events['risk_events']}",
        f"kill_switch_hits={risk_events['kill_switch_hits']}",
        "positions:",
    ]
    if not positions:
        lines.append("  none")
    else:
        for row in positions:
            lines.append(
                f"  {row['symbol']}: side={row['side']} qty={row['quantity']} "
                f"avg={row['avg_price']:.4f} realized={row['realized_pnl']:.4f} "
                f"unrealized={row['unrealized_pnl']:.4f}"
            )
    connection.close()
    return "\n".join(lines)
