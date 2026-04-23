# IBKR 美股综合策略设计说明书

> 本文档是 IBKR 美股策略的**总纲**。
> 对 12 套公开策略思想（见 §1.4）做了取舍整合，形成 **Layer 1 秒级 scalp + Layer 2 分钟级统计套利**的两层一体设计。
> Layer 1 的详细规格见 [IBKR_US_Equities_Micro_Alpha_Scalp_Design.md](IBKR_US_Equities_Micro_Alpha_Scalp_Design.md)。

---

## 1. 策略总览

### 1.1 定位

`IBKR US Equities Multi-Layer Micro/Stat-Arb Engine`

在 IBKR 非共置、非低延迟环境下，同时运行两套互补的 alpha：

- **Layer 1**：微观结构秒级 scalp，目标 `1~2 tick`，持仓 `1~15s`
- **Layer 2**：分钟级统计套利（pair/ETF basis 均值回归），目标 `10~50 bps`，持仓 `5~60 min`

两层共享同一套行情归一化、风控熔断、仓位预算和 regime 切换。

### 1.2 核心理念

- 不追求微秒级抢跑，只寻找在当前延迟下仍具正期望的最短可盈利持有周期
- 不做纯 maker 做市（IBKR 零售无 rebate、无 colo）；借鉴 Avellaneda/GLFT 的 reservation price 思想用于 taker 入场判断和 passive-improvement 的库存 skew
- 不做跨所搬砖、不做加密、不做 FX、不做期货趋势（这些思想作为参考但不落地）
- 信号先合成 fair value，再决定入场/报价；不孤立使用单一指标

### 1.3 明确不做的事

| 不做 | 原因 |
|---|---|
| 纯 maker 双边做市 | IBKR 零售无 rebate、被反选风险高于收益 |
| 跨所套利、三角套利 | 标的是美股现货单所，无此结构 |
| FX / 加密 / 期货 | 超出本仓库标的范围 |
| 毫秒级 HFT 抢跑 | 非共置环境打不过专业玩家 |
| 日内单边趋势跟踪 | 本期暂不做，保留为 L3 扩展位 |

### 1.4 12 策略取舍总表

| # | 来源策略 | 处置 | 用在哪里 | 来源 |
|---|---|---|---|---|
| 1 | Order Imbalance（OBI/OFI/Tape/microprice） | **采纳** | Layer 1 核心信号，已在 [signals.py](../src/ibkr_micro_alpha/signals.py) 实现 | txt §1–§15 |
| 2 | LOBCAST / lob-deep-learning | **可选采纳** | Layer 1 可插拔 score fusion hook（默认权重 0） | txt LOB-DL 节 |
| 3 | limit-order-book-market-making（纯 MM） | **舍弃** | IBKR 零售不做纯做市 | txt MM 节 |
| 4 | Hummingbot（XEMM/AMM/LP/Hedge） | **舍弃** | 加密生态 | txt Hummingbot 节 |
| 5a | hftbacktest - Fair Value / Pricing Framework | **采纳** | §4 公平价引擎：`FV = microprice + β·OBI_tilt + γ·Basis` | txt hftbacktest §1.4–§1.6 |
| 5b | hftbacktest - Queue / latency realism | **采纳（回测侧）** | §12 回测设计原则 | txt hftbacktest §4–§5 |
| 5c | hftbacktest - APT 多因子 fair value | **借鉴** | Layer 2 用 APT 风格多因子连续 forecast | txt hftbacktest §1.6 |
| 5d | GLFT / Avellaneda reservation price + inventory skew | **借鉴（不做双边 MM）** | §4.3 Layer 1 passive-improvement 的 inventory skew | txt Avellaneda / hftbacktest §1.3 |
| 6 | TriangularArbitrage | **舍弃** | 加密单所三角套利 | txt TriangularArb 节 |
| 7 | MeanReversionAlgo（5 min z-score） | **采纳** | Layer 2 基础引擎 | txt MeanReversionAlgo 节 |
| 8 | RSI + MA 1 min EURUSD | **借鉴** | Layer 2 的"极端确认"思路（MA 定方向 + z-score 定极端） | txt RSI+MA 节 |
| 9 | jiansenzheng/oanda_trading（ADF regime switch） | **采纳** | §7 跨层 regime 模块 | txt oanda_trading 节 |
| 10 | PyTrendFollow（EWMAC + carry + vol target） | **借鉴（部分）** | §9 vol-target 仓位公式；§6 Layer 2 EWMAC 连续 forecast | txt PyTrendFollow 节 |
| 11 | rolling-panda-san/notebooks | **借鉴（分类）** | §6 pair/basket 策略族分类参考 | txt rolling-panda-san 节 |
| 12 | jironghuang/trend_following（Carver 风 Continuous Forecasts） | **借鉴** | §6 Layer 2 多信号 forecast scaling + capping + 加权合成 | txt jironghuang 节 |

---

## 2. 适用品种

### 2.1 Layer 1 scalp 标的（沿用现有 [configs/default.toml](../configs/default.toml)）

`AAPL, NVDA, AMD, MSFT, AMZN, META, SPY, QQQ`

条件：买一卖一长期连续、点差稳定在 `1~2 tick`、逐笔成交连续、不易被单笔打穿。

### 2.2 Layer 2 stat-arb pair/basket 池

| 组别 | 标的对 | 类型 | 适配性 |
|---|---|---|---|
| ETF 对 | SPY – QQQ | 指数相关 | 强协整，作为首发 pair |
| ETF 对 | XLK – QQQ | 科技板块对 | 强相关，但 β 漂移需监控 |
| ETF 对 | SMH – SOXX | 半导体 ETF | 高度协整 |
| 龙头成分 | NVDA – AMD | 板块内龙头 | 日内相关但事件驱动易断 |
| 龙头成分 | AAPL – MSFT | 大盘权重股 | 弱相关，仅实验 |
| ETF-成分股 | QQQ 成分股 vs QQQ | basis 策略 | 研究阶段，后期扩展 |

Layer 2 启动白名单：`SPY-QQQ`、`NVDA-AMD`（两对先上 shadow）。

### 2.3 避开场景（两层通用）

- 开盘 5 min、收盘 5 min 单独降档
- 财报前后 24h、FOMC/CPI/NFP 当天：两层都关闭
- 个股新闻异常窗口：单票级 kill
- 点差 `> max_spread_kill_ticks`、quote 陈旧 `> stale_quote_kill_ms`：两层都停
- 日内波动率 `> 95th percentile`（历史分位）：Layer 2 强制 flat

---

## 3. 数据与事件模型

### 3.1 沿用现有事件流（Layer 1 已实现）

事件类型（见 [types.py](../src/ibkr_micro_alpha/types.py)）：

- `BookUpdate` / `QuoteUpdate` / `TradePrint` / `MarketMetaUpdate` / `StatusUpdate`

字段归一化参见 [IBKR_US_Equities_Micro_Alpha_Scalp_Design.md §4](IBKR_US_Equities_Micro_Alpha_Scalp_Design.md)。

### 3.2 Layer 2 新增事件（后续实现）

| 事件 | 频率 | 内容 |
|---|---|---|
| `FairValueUpdate` | 每次 quote/book 更新 | `symbol, fair_value, fv_components, ts` |
| `PairSpreadUpdate` | 1 Hz（聚合） | `pair_id, spread_t, z_spread, beta, cointegration_p, ts` |
| `RegimeChangeEvent` | 状态切换时 | `symbol / pair_id, old_regime, new_regime, confidence, reason` |

### 3.3 时间窗口分层

- 微秒 / 毫秒窗口：Layer 1 信号用（沿用现有 `trade_window_ms=1000`, `quote_window_ms=1000`, `microprice_window_ms=800`）
- 秒级窗口：`1s / 5s / 10s` — Layer 2 pair spread 聚合
- 分钟级窗口：`1min / 5min / 15min / 30min` — Layer 2 EWMAC、z-score、协整检验
- 日级窗口：每日开盘前重新拟合 pair β、更新波动率 target

---

## 4. Fair-Value 引擎（Layer 1 新增）

### 4.1 单标的 fair value

公式：

```
FV_t = microprice_t + β_obi · OBI_tilt_t · TickSize + β_basis · Basis_t · TickSize
```

其中：

- `microprice_t`：size-weighted mid（已在 [signals.py](../src/ibkr_micro_alpha/signals.py) `microprice()` 中实现）
- `OBI_tilt_t`：多档加权不平衡（复用 `weighted_imbalance`），已做 z-score 归一化
- `Basis_t`：个股相对基准（SPY 或板块 ETF）的归一化短期收益差（新），定义：

```
Basis_t = Return_symbol(τ) - Return_benchmark(τ)    (τ = 30s 窗口)
```

- `β_obi`, `β_basis`：回归拟合得到的偏移系数（ticks 量级），初始可用启发值 `β_obi = 0.3`, `β_basis = 0.2`

### 4.2 Layer 1 入场使用方式

CONFIRMED_TAKER 入场在现有阈值（见 [§5.1 配置](#511-layer-1-现有参数不改)）基础上增加过滤：

- 做多要求：`(FV_t - mid_t) / TickSize ≥ fv_min_tilt_ticks`（初值 `0.3`）
- 做空要求：`(FV_t - mid_t) / TickSize ≤ -fv_min_tilt_ticks`

也就是：**公平价必须与打单方向一致且偏移足够大**，否则认为打单是接最后一棒。

### 4.3 Passive-Improvement 的 inventory skew（借鉴 Avellaneda）

当已有同方向库存时，Layer 1 的 PASSIVE_IMPROVEMENT 挂单位置从 FV 出发做 skew：

```
reservation_price = FV_t - skew_factor · inventory_ratio · TickSize
inventory_ratio = current_qty / max_symbol_qty    (range [-1, +1])
skew_factor = 0.5    (初始值，tunable)
```

效果：持多仓时 reservation 下移 → bid 更不积极、减少继续追多；对称做空同理。
**不做双边持续报价，仍然是"等待时机的 passive entry"**。

### 4.4 Basis 计算依赖的现有抽象

- 现有 [signals.py](../src/ibkr_micro_alpha/signals.py) 的 `_linkage_score()` 已经有"多标的方向分数"的框架，但它是**方向归一化分数**，不是价差
- 新引擎 `fair_value.py` 需要独立跟踪基准标的的 `mid_price` 滚动收益（可新增 `state.py` 中的 `benchmark_return_window`）
- 配置新增 `[symbols.*].fv_benchmark_symbol` 字段（默认 `SPY` 对大盘股，`QQQ` 对科技股，ETF 自身 `None`）

---

## 5. Layer 1：秒级 Scalp

详见 [IBKR_US_Equities_Micro_Alpha_Scalp_Design.md](IBKR_US_Equities_Micro_Alpha_Scalp_Design.md)。下为本综合文档的增量与联动说明。

### 5.1.1 Layer 1 现有参数（不改）

主要沿用 [configs/default.toml](../configs/default.toml) 的 `[strategy]` 段：

- `confirmed_entry_threshold = 2.25`, `passive_entry_threshold = 1.75`
- `confirmed_min_signal_agree = 4`
- `score_collapse_threshold = 0.50`, `soft_hold_ms = 3000`, `hard_hold_ms = 12000`
- `max_payup_ticks = 1.0`, `tp_ticks = 1.0`, `strong_tp_ticks = 2.0`
- `max_spread_ticks = 2.0`, `volatility_guard_ticks = 8.0`
- Weights 沿用现有 `l1_imbalance=0.15, quote_ofi=0.20, tape_ofi=0.20, trade_burst=0.15, microprice_tilt=0.15, microprice_momentum=0.15, linkage=0.25, depth_bonus=0.30`

### 5.2 Layer 1 新增：Fair-Value 一致性过滤

增补参数（新段 `[strategy.fair_value]`）：

- `fv_enabled = true`
- `fv_min_tilt_ticks = 0.3`  — 公平价方向偏移最小值
- `beta_obi = 0.3`            — OBI 系数
- `beta_basis = 0.2`           — basis 系数
- `fv_skew_factor = 0.5`       — passive-improvement 的 inventory skew 因子

### 5.3 Layer 1 联动 Layer 2 的退出规则（新）

当 Layer 2 对同一标的持有 pair 腿时：

- Layer 1 **不得新开** 与 Layer 2 pair 腿方向相反的仓位（避免自打架）
- Layer 1 可以开同向仓位，但单票总净头寸受 `symbol_net_exposure_cap` 约束

这条规则的实现位点：风控模块 `risk.py` 的 `evaluate()` 中新增一个 `_cross_layer_conflict_guard()` 方法。

### 5.4 Layer 1 伪代码（简化版，完整版见 scalp 设计文档）

```text
on_quote_or_trade_event(event):
    update_market_state(event)
    signals = compute_signals(symbol)            # 已有 8 个
    fv = compute_fair_value(symbol)              # 新
    filters = build_filters(symbol) + fv_filter(fv)  # 新增 FV 一致性
    regime = regime_state.current(symbol)        # 新, §7

    if regime == ABNORMAL:
        return
    if has_position(symbol):
        manage_open_position(...)
        return
    if not filters.all_ok:
        return

    long_score, short_score = compute_scores(signals, filters, regime)
    if candidate := determine_entry_regime(long_score, short_score, filters, fv):
        if not risk.cross_layer_ok(symbol, candidate.side):
            return
        send_entry(candidate)
```

---

## 6. Layer 2：分钟级统计套利（核心新增）

### 6.1 架构

- 新模块 `pair_basket.py`（Phase 2 实现）
  - 维护每个 pair 的价差序列（每秒采样一次）
  - 每 10s 重算 z-score、每日开盘重拟合 β、每 30min 重新做协整检验（ADF）
- 新事件 `PairSpreadUpdate`、`RegimeChangeEvent`
- Layer 2 决策层 `pair_strategy.py` 订阅 `PairSpreadUpdate` + `RegimeChangeEvent`
- Layer 2 执行复用现有 `execution.py`，但**每笔交易必须同时成两条腿**（leg-A 与 leg-B 通过同一 `pair_order_group_id` 捆绑）

### 6.2 信号层

#### 6.2.1 价差定义

```
spread_t = log(P_A_t) - β · log(P_B_t) - μ
```

其中：

- `β`：每日开盘前用过去 5 个交易日的分钟 close 做 OLS 拟合（先验）
- `μ`：spread 的长期均值（滚动 30min 窗口）
- 使用 log 以保证比例不变性（5% 涨跌不管价位高低等价）

#### 6.2.2 z-score 均值回归信号

```
z_spread_t = (spread_t - MA_N(spread)) / σ_N(spread)
N = 30 min 滚动窗口
```

参考 MeanReversionAlgo（5min z-score）思路，但用 1s 采样、30min 窗口、更敏感。

#### 6.2.3 EWMAC 连续 forecast（借鉴 PyTrendFollow / jironghuang）

对 spread 本身做三组 EWMAC：

```
ewmac(fast, slow) = EMA_fast(spread) - EMA_slow(spread)
raw_forecast(fast, slow) = ewmac / vol_spread
```

三档：`(8, 32), (16, 64), (32, 128)` 分钟。

合成（Carver 风）：

```
scaled_i = raw_forecast_i · scalar_i   (scalar 使 |scaled| 平均 ≈ 10)
combined_forecast = Σ w_i · scaled_i
combined_forecast = clip(combined_forecast, -20, +20)
```

初始权重等权 `1/3`；`scalar` 用离线回测标定。

#### 6.2.4 RSI on spread（借鉴 RSI+MA 思路）

```
rsi_spread = RSI(spread, window=14 * 60s)
```

作为"极端确认"辅助指标。不作为主信号，仅在入场时要求 `rsi_spread > 70`（做空 pair）或 `< 30`（做多 pair）。

### 6.3 入场条件

做多 pair（买 A 卖 B）：

1. `z_spread_t ≤ -z_entry_threshold`（初值 `-2.0`）
2. `combined_forecast > 0`（趋势方向与回归一致）
3. `rsi_spread < 30`
4. `regime ∈ {MEAN_REVERTING, NEUTRAL}`（§7 judges）
5. `cointegration_p < 0.05`（30min 前最新 ADF 结果）
6. Layer 2 当日未触发熔断
7. 该 pair 当前无持仓

做空 pair（卖 A 买 B）对称。

### 6.4 出场条件

任一触发即平仓：

- 正常止盈：`|z_spread_t| ≤ z_exit_threshold`（初值 `0.5`）
- 止损：`|z_spread_t| ≥ z_stop_threshold`（初值 `3.5`）— 结构破坏
- 协整失效：最近 ADF `p ≥ 0.10` 连续 3 次
- 持仓超时：`holding_time ≥ max_hold_min`（初值 `45 min`）
- 跨层联动：Layer 1 检测到同标的暴力方向行情（5min realized vol 分位 > 95）→ 强制减仓
- 市场状态熔断（见 §10）
- EOD 强制平仓（收盘前 15 min 开始清仓）

### 6.5 执行

- 两条腿**同时**用 `marketable limit`（入场）或 `limit`（出场）提交
- 成交确认：要求两腿都成交才算 pair 开仓成功；如只成一腿 → 立即撤剩余腿 + flatten 已成腿（`execution.py` 的 "leg-fail cleanup"）
- 仓位比例：`qty_A · P_A = β · qty_B · P_B`，即两腿等金额（β 调整）

### 6.6 Layer 2 伪代码

```text
on_pair_spread_update(pair_id, spread, z_spread, forecast, rsi, adf_p):
    regime = regime_state.current(pair_id)
    if regime == ABNORMAL or layer2_killed:
        return

    if has_pair_position(pair_id):
        check_exit_conditions(pair_id, spread, z_spread, adf_p)
        return

    if adf_p >= 0.05: 
        return   # 非协整，不新开
    if regime not in {MEAN_REVERTING, NEUTRAL}:
        return

    if z_spread <= -2.0 and forecast > 0 and rsi < 30:
        open_pair_long(pair_id)
    elif z_spread >= 2.0 and forecast < 0 and rsi > 70:
        open_pair_short(pair_id)
```

---

## 7. Regime Switch（跨层）

### 7.1 输入指标

新模块 `regime.py`（Phase 3 实现），维护以下状态：

| 指标 | 计算 | 用途 |
|---|---|---|
| `adf_stat` | spread 滚动 30min ADF 统计量 | Layer 2 pair 是否处于均值回归态 |
| `vol_regime` | 过去 5min realized vol 的历史分位（0–100） | 超过 90 分位 → 高波动态 |
| `trend_strength` | 5min EWMAC(8,32) 幅度 / vol | 绝对值大 → 趋势态 |
| `spread_health` | 点差中位数相对历史分位 | 超宽 → 异常态 |

### 7.2 状态机

四态：`TREND`, `MEAN_REVERTING`, `NEUTRAL`, `ABNORMAL`

```
if spread_health 异常 OR vol_regime > 95 OR 数据流异常:
    state = ABNORMAL
elif adf_stat 显著（p < 0.05）AND vol_regime < 70:
    state = MEAN_REVERTING
elif |trend_strength| > trend_threshold AND vol_regime < 90:
    state = TREND
else:
    state = NEUTRAL
```

### 7.3 两层联动

| 状态 | Layer 1 scalp | Layer 2 stat-arb |
|---|---|---|
| `TREND` | 入场阈值**放宽** `-10%`（捕捉方向延续） | **关闭新开仓**（均值回归假设失效） |
| `MEAN_REVERTING` | 入场阈值**收紧** `+10%`（动量弱） | **开启**（主战场） |
| `NEUTRAL` | 沿用默认阈值 | 沿用默认阈值 |
| `ABNORMAL` | **停止新开仓** | **停止新开仓 + 主动平仓** |

### 7.4 切换平滑

- 新状态需**持续 ≥ 2 min** 确认才切换（避免 flip-flop）
- 从 `ABNORMAL` 退出需连续 5 min 正常
- 每次切换发布 `RegimeChangeEvent` 事件用于审计

### 7.5 Regime 范围

- Layer 1：regime 按**单标的**维度维护（AAPL 的 regime 与 NVDA 独立）
- Layer 2：regime 按**pair** 维度维护（SPY-QQQ 与 NVDA-AMD 独立）

---

## 8. 深度学习信号融合（可选，实验层）

### 8.1 目标

保留 LOBCAST / DeepLOB 思路的接入口，不作为本期主路径。

### 8.2 设计

- 输入：T=100 的 LOB 序列（10 档 × 4 = 40 features × 100 time steps）
- 输出：`P(U), P(S), P(D)` 三分类分布
- 融合方式：

```
score_ml = P(U) - P(D)    (∈ [-1, +1])
long_score_total = long_score_manual + w_ml · z(score_ml)
```

默认 `w_ml = 0.0`（off），需要离线训练且验证稳定后才手工调高。

### 8.3 基础设施要求

- 离线 LOB 数据采集 pipeline（已有 Parquet 审计可复用）
- 训练 / 评估框架（PyTorch + FI-2010 格式数据转换器）
- 生产推理：CPU float32 单例模型，每 500ms 推理一次（不是逐笔）

### 8.4 本期处置

仅在 `signals.py` 预留 hook（不落代码）：

- `SignalSnapshot` 增加 `ml_score: float | None` 字段
- 权重配置增加 `weights.ml_score = 0.0`
- Phase 5 再实现

---

## 9. 仓位管理（跨层，借鉴 PyTrendFollow vol-target）

### 9.1 账户级目标

- `vol_target_annual = 8%`（保守起步，远低于 PyTrendFollow 默认 12.5%）
- `daily_vol_target$ = capital · vol_target_annual / sqrt(252)`

### 9.2 Layer 分配

| Layer | 风险预算占比 | 备注 |
|---|---|---|
| Layer 1 scalp | 60% | 换手高、持仓短 |
| Layer 2 stat-arb | 40% | 持仓久但暴露更低 |
| 预留 | 0% | 未来 L3（日内趋势） |

### 9.3 单笔仓位公式

Layer 1（沿用现有但增加 vol-target 上限）：

```
size_raw = min(
    risk_budget_per_trade / max(stop_cents, spread_cents, vol_floor_cents),  # 现有
    depth_participation_rate · min(top_bid_depth, top_ask_depth),             # 现有
    daily_vol_target$ · 0.6 / (N_active_symbols · symbol_daily_vol$),         # 新: vol-target
    max_shares_per_order, 
    symbol_max_shares
)
```

Layer 2（新）：

```
forecast_pair = clip(combined_forecast, -20, +20)   # §6.2.3
leg_A_notional = (forecast_pair / 10) · (daily_vol_target$ · 0.4) / spread_vol_per_day
leg_B_notional = leg_A_notional · β
qty_A = round(leg_A_notional / P_A)
qty_B = round(leg_B_notional / P_B)
```

`spread_vol_per_day` 是 spread 的日收益波动率（30 日 EWMA），作为分母让 spread 波动小的 pair 放大仓位、波动大的压缩。

### 9.4 组合层面 vol 再归一（借鉴 PyTrendFollow 的 `vol_norm`）

```
realized_daily_vol_account = EWMA(account PnL daily std, halflife=30d)
vol_scalar = clip(vol_target_annual / (realized_daily_vol_account · sqrt(252)), 0.0, 1.5)
all_layer_sizes *= vol_scalar
```

当账户最近波动突增，`vol_scalar` 自动 < 1 压缩仓位；平稳时允许最多 1.5 倍放大。

### 9.5 单票净暴露上限

```
symbol_net_exposure$ = |Layer1_position + Σ Layer2 pair legs涉及该标的|
                     ≤ symbol_net_exposure_cap$
```

`symbol_net_exposure_cap$` 建议 = `2 · Layer1 单笔仓位金额`。

---

## 10. 风控（分层 + 跨层）

### 10.1 Layer 1 现有（沿用 [risk.py](../src/ibkr_micro_alpha/risk.py)）

- 健康检查：connection_lost, data_stale, spread 熔断, session kill_switch
- 会话级：`max_strategy_daily_loss = -400`, `max_symbol_daily_loss = -150`, `max_consecutive_losses = 4`, `max_open_positions = 3`
- 入场：pending_orders, missing_signal, market_ok
- 做空库存：`min_shortable_tier = 2.5`, `min_shortable_shares_multiple = 5`
- 数量限制：`max_order_qty = 50`, `max_symbol_qty = 100`, `canary_qty = 1`（LIVE 模式）

### 10.2 Layer 2 新增（pair 层）

| 控制项 | 初值 | 说明 |
|---|---|---|
| `pair_max_concurrent` | 5 | 最多同时持有 5 个 pair |
| `pair_max_per_id` | 2 | 单个 pair 最多持 2 组 |
| `pair_cointegration_p_kill` | 0.10 | 连续 3 次 ADF p ≥ 0.10 → 该 pair 当日停 |
| `pair_z_spread_panic` | 5.0 | |z_spread| 超过此值立即 flatten |
| `pair_daily_loss_cap` | -200 | Layer 2 总日亏上限（独立于 Layer 1） |
| `pair_max_hold_min` | 45 | 持仓超时 |
| `leg_fail_max_retries` | 2 | 只成一腿的清理重试上限 |

### 10.3 跨层共享

- **账户级 daily loss**：`max_account_daily_loss = -500`（= Layer 1 -400 + Layer 2 -200 不能都打满，留 100 缓冲。超过则全部 flatten）
- **单票净暴露**：§9.5 的 `symbol_net_exposure_cap$`，在 `risk.py` 的 `evaluate()` 中新增 `_net_exposure_guard()`
- **跨层冲突**：§5.3 的规则（Layer 1 不能逆 Layer 2 腿方向开单）
- **统一 kill switch**：任一层的 kill_switch 触发会升级为"新开仓全停"但不自动 flatten（由操作员决定）
- **ABNORMAL regime**：两层都停止新开仓，Layer 2 主动平仓

### 10.4 日内时段熔断

- 开盘前 5 min（09:30–09:35）：两层都关闭
- 收盘前 15 min（15:45–16:00）：Layer 2 强制平仓、Layer 1 只允许减仓
- 午盘低流动时段（12:00–13:00）：两层阈值收紧 +20%

---

## 11. 参数表

### 11.1 Layer 1 新增参数（`configs/default.toml` 的 `[strategy.fair_value]` 段）

| 参数 | 含义 | 初值 |
|---|---|---|
| `fv_enabled` | 启用 FV 一致性过滤 | `true` |
| `fv_min_tilt_ticks` | FV 方向最小偏移 | `0.3` |
| `beta_obi` | OBI tilt 到 FV 的系数 | `0.3` |
| `beta_basis` | Basis 到 FV 的系数 | `0.2` |
| `fv_skew_factor` | passive-improvement inventory skew 因子 | `0.5` |
| `fv_basis_window_sec` | Basis 短期收益窗口 | `30` |
| `fv_regress_halflife_days` | β_obi/β_basis EWMA 半衰期 | `5` |

### 11.2 Layer 2 新参数（新段 `[pairs]` 与 `[pairs.<pair_id>]`）

| 参数 | 含义 | 初值 |
|---|---|---|
| `pair_enabled` | Layer 2 开关 | `false`（shadow 验证完才开） |
| `spread_sample_hz` | spread 采样频率 | `1` Hz |
| `z_window_min` | z-score 滚动窗口 | `30` |
| `z_entry_threshold` | 入场 z 阈值 | `2.0` |
| `z_exit_threshold` | 止盈 z 阈值 | `0.5` |
| `z_stop_threshold` | 止损 z 阈值 | `3.5` |
| `max_hold_min` | 最长持仓 | `45` |
| `cointegration_refresh_min` | ADF 重检间隔 | `30` |
| `beta_refit_time` | 每日 β 重拟合时间 | `09:00 ET` |
| `rsi_window_min` | RSI 窗口 | `14` |
| `rsi_overbought` | RSI 超买 | `70` |
| `rsi_oversold` | RSI 超卖 | `30` |
| `ewmac_fast_slow_pairs` | EWMAC 三档 | `[[8,32],[16,64],[32,128]]` |
| `forecast_cap` | forecast 截断上限 | `20` |
| `pair_max_concurrent` | 最多并行 pair | `5` |
| `pair_max_per_id` | 单 pair 最多组数 | `2` |

### 11.3 Regime 参数（新段 `[regime]`）

| 参数 | 含义 | 初值 |
|---|---|---|
| `adf_window_min` | ADF 窗口 | `30` |
| `adf_pvalue_threshold` | ADF p 阈值 | `0.05` |
| `vol_regime_window_min` | vol regime 窗口 | `5` |
| `vol_high_pctile` | 高波动阈值 | `90` |
| `vol_abnormal_pctile` | 异常波动阈值 | `95` |
| `trend_strength_threshold` | 趋势强度阈值 | `1.0` |
| `state_confirm_min` | 状态切换确认时间 | `2` |
| `abnormal_recovery_min` | 异常态恢复时间 | `5` |

### 11.4 Vol-Target 参数（新段 `[vol_target]`）

| 参数 | 含义 | 初值 |
|---|---|---|
| `vol_target_annual` | 年化目标波动 | `0.08` |
| `layer1_budget_pct` | Layer 1 风险预算占比 | `0.60` |
| `layer2_budget_pct` | Layer 2 风险预算占比 | `0.40` |
| `vol_scalar_min` | vol 归一最小值 | `0.0` |
| `vol_scalar_max` | vol 归一最大值 | `1.5` |
| `symbol_net_exposure_cap_usd` | 单票净暴露上限 | `5000` |

### 11.5 跨层风控（新段 `[risk.cross_layer]`）

| 参数 | 含义 | 初值 |
|---|---|---|
| `max_account_daily_loss` | 账户日亏上限 | `-500` |
| `pair_daily_loss_cap` | Layer 2 日亏上限 | `-200` |

---

## 12. 回测与实盘监控

### 12.1 回测设计原则（借鉴 hftbacktest）

- 事件驱动回放，区分 `下单 → 确认 → 成交 → 撤单` 四阶段延迟
- 区分 marketable limit 与 passive limit 的成交模型
- 模拟队列位置（保守版：只在真实成交发生时前进）
- 考虑手续费 + SEC fees + FINRA 交易活动费（做空 TAF）
- Layer 2 额外模拟：双腿原子性失败、单腿挂单 TTL

### 12.2 Layer 2 特定回测

- pair 协整回滚：每日/每周 β 重拟合是否维持稳健
- z_spread 进出模拟：从入场到 `|z| < 0.5` 的实际持仓时长分布
- 事件冲击：财报/新闻日的 pair 表现（应被 regime 过滤掉，回测时标记这些日）

### 12.3 关键评估指标（两层合并 + 分层）

**合并层**：
- 组合 Sharpe / Sortino
- 最大回撤、日最大回撤
- 相关性：Layer 1 vs Layer 2 日收益相关（目标 < 0.3，说明真正互补）

**Layer 1**：
- 单笔期望、胜率、盈亏比（已有）
- markout 曲线 100ms / 500ms / 1s / 3s / 5s / 10s
- FV 一致性触发率、命中率

**Layer 2**：
- 每 pair 月度 PnL 贡献、Sharpe
- 平均持仓分钟数
- 入场 z-score 分布、出场 z-score 分布
- 协整破坏事件 / 被 regime 过滤事件比例

### 12.4 实盘监控仪表盘（新增 Layer 2 面板）

**行情健康**（沿用）：数据延迟、缺包率、时钟漂移

**信号健康**：
- Layer 1：各信号 z-score 分位、FV 偏移分位
- Layer 2：z_spread 分布、β 漂移（每日绘图）、ADF p-value 轨迹

**执行健康**：
- Layer 1：成交率、滑点、TP 命中率（已有）
- Layer 2：双腿同时成交率、leg-fail cleanup 次数、EOD 强平次数

**交易表现**：
- 分层 PnL、每分钟成交笔数（分层）
- markout 曲线（分层）

**Kill Switch**：
- 任一层熔断 → 面板红色告警
- Regime = ABNORMAL → 面板黄色告警

---

## 13. 落地优先级（附录，供后续实现）

| Phase | 目标 | 关键文件 | 预估工作量 | 前置依赖 |
|---|---|---|---|---|
| **Phase 1** | FV 引擎（单标的） | 新增 [fair_value.py](../src/ibkr_micro_alpha/fair_value.py)；扩展 [types.py](../src/ibkr_micro_alpha/types.py) 增 `FairValueUpdate`；[signals.py](../src/ibkr_micro_alpha/signals.py) 增 `FV` 一致性过滤；[config.py](../src/ibkr_micro_alpha/config.py) 增 `[strategy.fair_value]` | 3–5 天 | 现有 scalp 引擎 |
| **Phase 2** | Layer 2 pair 引擎（SPY-QQQ + NVDA-AMD 先 shadow） | 新增 [pair_basket.py](../src/ibkr_micro_alpha/pair_basket.py)；扩展 [types.py](../src/ibkr_micro_alpha/types.py) 增 `PairSpreadUpdate`；[config.py](../src/ibkr_micro_alpha/config.py) 增 `[pairs]`；[execution.py](../src/ibkr_micro_alpha/execution.py) 支持 pair 双腿原子提交 | 7–10 天 | Phase 1 |
| **Phase 3** | Regime 模块 + 跨层联动 | 新增 [regime.py](../src/ibkr_micro_alpha/regime.py)；扩展 [strategy.py](../src/ibkr_micro_alpha/strategy.py) 订阅 regime 事件调阈值；[risk.py](../src/ibkr_micro_alpha/risk.py) 新增 `_cross_layer_conflict_guard()` | 5–7 天 | Phase 2 |
| **Phase 4** | vol-target 跨层仓位 | 新增 [vol_engine.py](../src/ibkr_micro_alpha/vol_engine.py)；[strategy.py](../src/ibkr_micro_alpha/strategy.py) 用 vol_scalar 调节仓位；[risk.py](../src/ibkr_micro_alpha/risk.py) 加 `symbol_net_exposure_cap` | 4–6 天 | Phase 3 |
| **Phase 5（可选）** | LOB-DL 信号融合 | 新增 `ml_signal.py` + 离线训练 notebook；`SignalSnapshot.ml_score` | 2–3 周 | Phase 4 + 离线数据 |

**Phase 间原则**：

- 每个 Phase 独立可回退（feature flag 控制）
- 每个 Phase 新增模块必须带单元测试（延续 [tests/](../tests/) 现有风格）
- Phase 2 完成后必须 shadow 至少 10 个交易日才允许 LIVE
- LIVE 开启时先用 `canary_qty = 1` 跑 2 个交易日再放量

---

## 14. 关键结论与风险

### 14.1 本文档的定位

- **这是设计总纲，不是实现**。所有"新增模块""新增字段""新增事件"均为未来 Phase 目标
- 现有代码（[src/ibkr_micro_alpha/](../src/ibkr_micro_alpha/)）本期**不改动**
- Layer 1 的详细规格以 [IBKR_US_Equities_Micro_Alpha_Scalp_Design.md](IBKR_US_Equities_Micro_Alpha_Scalp_Design.md) 为准，本文档的 §5 只是汇总和与 Layer 2 联动的增补

### 14.2 必须警惕的风险

1. **Layer 2 协整失效**：美股 pair 关系受事件驱动（财报、收购、监管）破坏的频率远高于 FX。必须严格执行每日 β 重拟合 + ADF 重检 + 协整破坏即停
2. **Regime 切换滞后**：2 min 确认时间保护了 flip-flop，但可能让异常行情多跑 2 min。`ABNORMAL` 态的 `vol_abnormal_pctile = 95` 必须结合硬阈值（spread_ticks > 4）作保险
3. **跨层自打架**：Layer 1 短多 + Layer 2 pair 腿卖同票 会自我对冲。§5.3 的 cross-layer guard 是**硬约束**，不是软建议
4. **vol-target 放大风险**：`vol_scalar_max = 1.5` 在最平静时段会让所有仓位放大 1.5 倍。必须配合 `symbol_net_exposure_cap$` 硬顶
5. **FV β 漂移**：`β_obi`、`β_basis` 如果长期不校准会偏离真实。建议每周离线回归一次，或做在线 EWMA 估计（halflife=5 日）

### 14.3 何时算这份设计"可以进入 Phase 1"

满足三个条件即可：

1. 用户阅读并确认 `12 策略取舍总表`（§1.4）无遗漏、无错判
2. Layer 1 → Layer 2 的数据流、事件流清楚（§3、§6、§7）
3. 风控跨层路径（§10）逻辑闭环

### 14.4 完整性自检清单

- [x] 每个来源策略都在 §1.4 总表里有明确归宿
- [x] Layer 1 + Layer 2 都给出伪代码骨架（§5.4、§6.6）
- [x] 每个新概念都注明 Phase（§13）
- [x] 参数表给了初值（§11）
- [x] 风控跨层路径闭环（§10.3）
- [x] 明确不做 maker 做市（§1.3、§1.4）
- [x] Regime 状态机与两层联动明确（§7）
- [x] vol-target 跨层仓位公式自洽（§9）
- [x] 回测设计引入队列/延迟现实主义（§12.1）
- [x] 引用现有代码模块路径正确（§5.1.1、§13）

---

## 15. 一句话总结

本策略是**"微观结构秒级 scalp（Layer 1）+ 统计套利分钟级 pair mean-reversion（Layer 2）"的双层 IBKR 美股引擎**，用共同的 Fair-Value 引擎、Regime 状态机、vol-target 仓位公式把两层黏合在一起；不做纯 maker 做市、不做跨所搬砖、不做 FX/期货/加密；从 12 套公开策略思想里**采纳** OBI/OFI/Tape/microprice、fair-value pricing、z-score mean reversion、ADF regime、Carver-style continuous forecast 和 vol-target；**舍弃** 纯 MM、三角套利、加密、期货趋势；**借鉴（不直搬）** Avellaneda skew、RSI+MA 极端确认、EWMAC 趋势连续化。落地按 5 个 Phase 渐进，每 Phase 独立可回退、带 feature flag、带单元测试。
