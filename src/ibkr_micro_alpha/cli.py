from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import load_engine_config
from .engine import build_report, flatten_book, reconcile_book, run_engine
from .types import EngineMode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ibkr-micro-alpha")
    parser.add_argument("command", choices=["capture", "shadow", "live", "flatten", "reconcile", "report"])
    parser.add_argument("--config", default=str(Path("configs") / "default.toml"))
    parser.add_argument("--date", dest="trading_date", default=None)
    return parser


def main() -> None:
    args = _parser().parse_args()
    mode_map = {
        "capture": EngineMode.CAPTURE,
        "shadow": EngineMode.SHADOW,
        "live": EngineMode.LIVE,
        "flatten": EngineMode.LIVE,
        "reconcile": EngineMode.LIVE,
        "report": EngineMode.SHADOW,
    }
    config = load_engine_config(args.config, mode=mode_map[args.command])
    if args.command in {"capture", "shadow", "live"}:
        asyncio.run(run_engine(config))
        return
    if args.command == "flatten":
        print(asyncio.run(flatten_book(config)))
        return
    if args.command == "reconcile":
        print(asyncio.run(reconcile_book(config)))
        return
    print(build_report(config, trading_date=args.trading_date))
