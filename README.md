# IBKR Micro Alpha

`IBKR Micro Alpha` 是一个面向 `IBKR + 美股高流动性标的` 的微观结构超短线引擎首版骨架。

当前仓库提供：

- `capture / shadow / live / flatten / reconcile / report` 六个命令入口
- `IBKR` 适配器接口与回调归一化骨架
- 五个微观结构信号与三类过滤器
- 单标的一次只持有一个净方向仓位的决策与执行框架
- `SQLite` 审计库与 `Parquet` 快照写出
- Linux `systemd` 部署样例

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
ibkr-micro-alpha shadow --config configs/default.toml
```

## Commands

- `ibkr-micro-alpha capture --config configs/default.toml`
- `ibkr-micro-alpha shadow --config configs/default.toml`
- `ibkr-micro-alpha live --config configs/default.toml`
- `ibkr-micro-alpha flatten --config configs/default.toml`
- `ibkr-micro-alpha reconcile --config configs/default.toml`
- `ibkr-micro-alpha report --config configs/default.toml --date 2026-04-22`

## Notes

- `live` 模式默认建议先以 `1 share canary` 运行。
- `Parquet` 写出依赖 `pyarrow`。
- `IBKR` 接口依赖本地 `IB Gateway` 或 `TWS`。
