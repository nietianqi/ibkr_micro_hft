# IBKR Micro Alpha

`IBKR Micro Alpha` 是一个面向 `IBKR + 美股高流动性标的` 的研究增强型微观结构超短线引擎。

当前仓库提供：

- `capture / shadow / live / flatten / reconcile / report` 六个命令入口
- `IBKR` 适配器接口与回调归一化骨架
- 微观结构主信号 + 时段分层 + higher-timeframe regime 增强
- `passive_improvement / confirmed_taker / aggressive_taker` 三种单主策略执行模式
- `NORMAL / QUEUE / ABNORMAL` 执行状态与 queue-defense / reservation-bias 过滤
- 单标的一次只持有一个净方向仓位的决策与执行框架
- `SQLite` 审计库与 `Parquet` 快照写出
- Linux `systemd` 部署样例

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python main.py shadow --config configs/default.toml
```

## Commands

- `python main.py capture --config configs/default.toml`
- `python main.py shadow --config configs/default.toml`
- `python main.py live --config configs/default.toml`
- `python main.py flatten --config configs/default.toml`
- `python main.py reconcile --config configs/default.toml`
- `python main.py report --config configs/default.toml --date 2026-04-22`

## Notes

- `live` 模式默认建议先以 `1 share canary` 运行。
- `Parquet` 写出依赖 `pyarrow`。
- `IBKR` 接口依赖本地 `IB Gateway` 或 `TWS`。
