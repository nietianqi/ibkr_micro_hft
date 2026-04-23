# IBKR 美股研究增强型微观结构双边策略说明书

## 1. 文档定位

本文档是当前仓库 `ibkr_micro_hft` 的主策略说明书，目标是把策略边界、配置结构、代码落点和上线顺序写清楚，便于继续实现、回测和复核。

这份文档只描述当前 v1 主干策略，不把研究想法混写成已经纳入实盘主链路的能力。

若出现冲突，以以下优先级为准：

1. 当前仓库已实现的代码行为
2. 本说明书
3. 外部研究备忘录或扩展提案

## 2. 策略总纲

本策略是一个 `IBKR 可执行`、`多空双边`、`以秒级微观结构 alpha 为核心` 的美股日内策略。

策略分为三层，但只有第一层负责真正触发交易：

1. 微观结构主策略
   使用 `Order Imbalance + Quote/LOB OFI + Tape OFI + microprice + linkage` 构造统一分数，负责秒级入场与退出。
2. CTA 式日内 regime / risk 增强
   只负责分时段过滤、方向偏置、仓位缩放和风险预算，不单独生成开仓信号。
3. LOB 深度学习研究层
   `LOBCAST / DeepLOB / TransLOB` 只保留为二期研究方向，不进入 v1 实盘硬门槛。

当前策略不是双引擎系统，也不是 `scalp + stat-arb pair engine` 的组合体。v1 只有一个统一的微观结构主策略。

## 3. v1 明确纳入与明确排除

| 项目 | v1 状态 | 说明 |
| --- | --- | --- |
| 统一微观结构主分数 | 纳入 | 当前主 alpha |
| `confirmed_taker` 入场 | 纳入 | 当前主入场方式 |
| `passive_improvement` 入场 | 纳入 | 仅在 `2-tick spread + depth available` 时启用 |
| `aggressive_taker` 入场 | 纳入 | 高确信度、短持有、默认 `1 tick` TP |
| `higher_tf_regime` | 纳入 | 只做加减分与仓位缩放 |
| `queue-defense / reservation bias` | 纳入 | 只作为执行过滤与缩量，不单独构成新策略 |
| 时段分层 `PRE/OPEN/CORE/CLOSE/POST` | 纳入 | 配置与风控均已对齐 |
| 标的分层 `Tier A / Tier B / Watchlist` | 纳入 | 交易权限与扩展时段权限都依赖它 |
| 扩展时段交易 | 纳入 | 但仅限允许的标的和时段 |
| 扩展时段做空限制 | 纳入 | 仅允许高流动性标的 |
| 动态仓位 sizing | 纳入 | 基于 `per_trade_risk_dollars`、点差、深度和时段上限 |
| pair stat-arb / basket engine | 排除 | 不属于当前主策略 |
| 协整、z-score 配对交易、两腿原子执行 | 排除 | 会引入全新执行与风控子系统 |
| 独立 `fair value` 引擎 | 排除 | 可作为未来研究，不是 v1 前置模块 |
| 独立 `vol-target` 账户层引擎 | 排除 | v1 仅保留单策略动态 sizing |
| LOB-DL 硬门槛 | 排除 | 只做二期研究型 `meta-filter` |
| 隔夜趋势持有 | 排除 | 当前策略只做日内 |

## 4. 标的范围与分层

### 4.1 默认交易池

默认交易池如下：

| 标的 | 层级 | 默认 live | 扩展时段 | 扩展时段做空 |
| --- | --- | --- | --- | --- |
| `SPY` | `Tier A` | 是 | 是 | 是 |
| `QQQ` | `Tier A` | 是 | 是 | 是 |
| `AAPL` | `Tier A` | 是 | 是 | 是 |
| `MSFT` | `Tier A` | 是 | 是 | 是 |
| `NVDA` | `Tier A` | 是 | 是 | 是 |
| `AMD` | `Tier B` | 是 | 否 | 否 |
| `AMZN` | `Tier B` | 是 | 否 | 否 |
| `META` | `Tier B` | 是 | 否 | 否 |

### 4.2 观察名单

`TSLA` 只作为观察名单候选，不纳入 v1 默认 live 交易池。若后续加入，应标记为 `watchlist` 或单独评估其盘前盘后质量后再升格。

### 4.3 分层原则

`Tier A`：

- 超高流动性 ETF / Mega-cap
- 允许 `RTH + 扩展时段`
- 扩展时段可做空，但仍需通过 IBKR shortability 检查

`Tier B`：

- 默认只做 `RTH`
- 盘前盘后只保留研究回放或影子模式观察
- v1 live 不允许扩展时段新开仓

`Watchlist`：

- 允许采集与观察
- 不允许 live/shadow 新开仓

## 5. 时段分层

策略按美东时间划分五个 session regime：

| Session | 时间 | v1 角色 |
| --- | --- | --- |
| `PRE` | `08:00-09:25 ET` | 允许交易，但阈值更高、仓位更小 |
| `OPEN` | `09:30-10:00 ET` | 允许交易，保留开盘流动性优势，但风控更紧 |
| `CORE` | `10:00-15:30 ET` | 主交易时段 |
| `CLOSE` | `15:30-16:00 ET` | 允许交易，但仓位回收 |
| `POST` | `16:00-17:30 ET` | 允许交易，但默认禁用被动改价入场、仓位显著缩小 |

`OFF` 状态下禁止新开仓。

## 6. 统一 alpha 结构

### 6.1 主信号

统一分数由以下主信号构成：

- `weighted_imbalance`
- `l1_imbalance`
- `quote_ofi`
- `lob_ofi`
- `tape_ofi`
- `trade_burst`
- `microprice_tilt`
- `microprice_momentum`

### 6.2 环境增强项

主信号之外，保留两类增强：

1. `reference linkage`
   将参考标的已有信号聚合为一个相对稳定的外部一致性分数。
2. `higher_tf_regime`
   用更高时间尺度的价格、点差、深度、成交速率和参考标的状态，为当前秒级 alpha 提供顺风或逆风偏置。

### 6.3 分数原则

策略不切换成“不同策略轮流上场”，而是始终使用一个统一分数：

```text
base_score
  = w1 * z(quote_ofi)
  + w2 * z(tape_ofi)
  + w3 * z(l1_imbalance)
  + w4 * z(trade_burst)
  + w5 * z(microprice_tilt)
  + w6 * z(microprice_momentum)

depth_adjustment
  = depth_bonus * 0.5 * (z(weighted_imbalance) + z(lob_ofi))

long_score
  = base_score + linkage_adjustment + depth_adjustment + higher_tf_adjustment

short_score
  = -base_score - linkage_adjustment - depth_adjustment - higher_tf_adjustment
```

其中：

- `linkage_adjustment = strategy.weights.linkage * linkage_score`
- `higher_tf_adjustment = session_config.higher_tf_bias_weight * higher_tf_regime_score`
- 只有在 `depth_available = true` 且启用了深度奖励时，才加入 `depth_adjustment`

## 7. higher_tf_regime 的职责与边界

`higher_tf_regime` 是“增强器”，不是“开仓器”。

它的输入来自三部分：

1. 符号自身的 `1m / 5m / 15m` 中期方向
2. 当前点差、盘口深度、成交速率相对历史基线的偏离
3. 参考 ETF 或关联大票的 `higher_tf_regime_score`

它的输出只做两件事：

1. 对统一主分数进行加减分
2. 对最终仓位进行顺风放大或逆风缩小

它不会：

- 单独触发开仓
- 取代微观结构 alpha
- 把策略变成分钟级趋势跟随系统

## 8. 入场逻辑

### 8.1 `confirmed_taker`

这是主入场路径，适用于盘口和成交都支持快速确认的场景。

触发条件：

- `long_score` 或 `short_score` 超过当前 session 的确认阈值
- 主信号同向数量达到最小一致性要求
- 市场质量通过
- 未过热
- 做空时 short inventory 通过
- 点差不超过允许阈值

执行方式：

- 使用 `marketable limit`
- 多头以 `ask + max_payup_ticks * tick_size`
- 空头以 `bid - max_payup_ticks * tick_size`

### 8.2 `passive_improvement`

这是次级入场路径，只在极少数适合挂单改良成交价的场景使用。

触发条件：

- 当前 session 允许被动入场
- `spread_ticks == 2`
- `depth_available = true`
- `passive_retry_available = true`
- 分数超过对应的被动阈值
- 多头需同时满足 `weighted_imbalance > 0` 且 `lob_ofi > 0`
- 空头需同时满足 `weighted_imbalance < 0` 且 `lob_ofi < 0`
- `reservation_bias` 需对挂单方向有利
- `QUEUE / ABNORMAL` 状态下禁用

执行方式：

- 多头挂在 `bid`
- 空头挂在 `ask`
- TTL 很短，默认 `250ms`
- 只允许一次重试

### 8.3 `aggressive_taker`

这是 `confirmed_taker` 的高确信度子路径，不是第二套策略。

触发条件：

- 统一分数显著高于常规确认阈值
- 六维方向一致性达到最高档
- `trade_burst` 活跃度明显放大
- `tape_ofi` 与 `microprice_tilt` 同向强化
- 当前必须处于 `NORMAL` 执行状态

执行方式：

- 仍使用 `marketable limit`
- 默认只吃 `1 tick`
- 最大持有时间显著短于普通 taker

## 9. 出场逻辑

### 9.1 收益性退出

默认使用 `maker take-profit`。

- 常规目标为 `1 tick`
- 当流状态明显更强时可提升至 `2 ticks`

### 9.2 保护性退出

当任一条件成立时，立即走 `taker/protective exit`：

- `score collapse`
- `quote/tape flip`
- `spread blowout`
- `stale quote`
- `soft hold timeout`
- `hard hold timeout`
- 市场质量恶化

当前实现里，多头与空头分别根据相反方向的分数崩塌和 `quote_ofi / tape_ofi` 翻转做保护性离场。

## 10. 风控与仓位

### 10.1 动态 sizing

基础 sizing 公式：

```text
size_raw
  = per_trade_risk_dollars
    / max(stop_cents, spread_cents, vol_floor_cents)

size_depth_cap
  = depth_participation_rate * min(top_bid_depth, top_ask_depth)

size_final
  = min(size_raw, size_depth_cap, session_scaled_cap, symbol_cap, order_cap)
```

其中：

- `stop_cents` 由点差与 tick 风险近似表示
- `spread_cents` 直接来自当前买一卖一点差
- `vol_floor_cents` 防止在极窄点差下仓位被放大得过头
- `session_scaled_cap` 由时段上限决定
- `symbol_cap` 和 `order_cap` 来自单票与单笔上限

### 10.2 时段仓位缩放

| Session | `size_scale` | 最大开仓数 |
| --- | --- | --- |
| `PRE` | `0.40` | `1` |
| `OPEN` | `0.75` | `2` |
| `CORE` | `1.00` | `3` |
| `CLOSE` | `0.75` | `2` |
| `POST` | `0.40` | `1` |
| `OFF` | `0.00` | `0` |

### 10.3 higher_tf 对仓位的影响

`higher_tf_regime_score` 会进一步微调最终数量：

- 顺风时轻微放大
- 逆风时缩小到约 `75%`
- 不会把一个原本不该开的仓位变成“允许开仓”

### 10.4 queue-defense 对仓位的影响

当盘口进入 `QUEUE` 状态时：

- 禁用 `passive_improvement`
- `confirmed_taker` 提高阈值与共振要求
- 最终数量再乘一次 `queue_size_scale`

### 10.5 执行状态

执行状态固定分为三种：

- `NORMAL`：正常允许被动或主动入场
- `QUEUE`：`1-tick spread` 或顶层排队偏薄时启用，缩量并提高确认
- `ABNORMAL`：只允许减仓、撤单、flatten

### 10.6 硬风控

硬风控包含：

- `max_order_quantity`
- `max_symbol_quantity`
- `max_open_positions`
- `max_symbol_daily_loss`
- `max_strategy_daily_loss`
- `max_consecutive_losses`
- `max_spread_kill_ticks`
- `stale_quote_kill_ms`
- `live canary quantity`

任何 kill switch 被触发时，停止继续新开仓，并由执行层完成取消和清仓动作。

## 11. 做空与扩展时段规则

### 11.1 常规时段做空

`RTH` 允许双边交易，但空头新开仓必须通过以下任一条件：

- `shortable_tier > min_shortable_tier`
- `shortable_shares >= quantity * min_shortable_shares_multiple`

### 11.2 扩展时段做空

只有同时满足以下条件，才允许盘前盘后空头新开仓：

- 当前 session 为 `PRE` 或 `POST`
- 符号 `allow_extended_hours = true`
- 符号 `allow_short_extended = true`
- short inventory 检查通过

因此，v1 默认只有 `Tier A` 高流动性标的可以在扩展时段尝试做空。

## 12. 代码结构映射

### 12.1 配置层

`src/ibkr_micro_alpha/config.py`

- `StrategyConfig.session_regimes`
- `RiskConfig.session_caps`
- `SymbolConfig.tier`
- `SymbolConfig.allow_extended_hours`
- `SymbolConfig.allow_short_extended`

### 12.2 时段判定

`src/ibkr_micro_alpha/session.py`

- `classify_session`
- `is_extended_hours`

### 12.3 信号层

`src/ibkr_micro_alpha/signals.py`

- 维护统一主分数
- 构造 `SignalFilterState`
- 计算 `higher_tf_regime_score`
- 生成 `entry_regime_candidate`

### 12.4 策略决策层

`src/ibkr_micro_alpha/strategy.py`

- 选择 `confirmed_taker` 或 `passive_improvement`
- 生成 `TradeIntent`
- 在持仓期间决定保护性退出

### 12.5 风控层

`src/ibkr_micro_alpha/risk.py`

- session gating
- 扩展时段权限控制
- 做空库存检查
- 动态数量裁剪
- kill switch

### 12.6 执行层

`src/ibkr_micro_alpha/execution.py`
`src/ibkr_micro_alpha/adapter/ibkr.py`

- live 模式透传 `outsideRth`
- 被动单 TTL 取消
- shadow 模式模拟成交
- take-profit 与 protective exit 的协同

## 13. 关键数据结构

### 13.1 `SignalSnapshot`

当前快照需至少包含：

- `session_regime`
- `higher_tf_regime_score`
- `session_trade_allowed`
- `shortable_tier`
- `shortable_shares`
- `entry_regime_candidate`

### 13.2 `SignalFilterState`

当前过滤状态需至少包含：

- `market_ok`
- `linkage_score`
- `overheat_long_ok`
- `overheat_short_ok`
- `quote_age_ms`
- `trade_rate_per_sec`
- `spread_ticks`
- `depth_available`
- `short_inventory_ok`
- `abnormal`
- `reasons`
- `session_reasons`

### 13.3 `DecisionContext`

决策上下文必须显式带上：

- `session_regime`
- `extended_hours`
- `short_inventory_ok`
- `shortable_tier`
- `shortable_shares`
- `passive_retry_available`

## 14. 默认配置原则

`configs/default.toml` 应体现以下原则：

1. session 参数单独维护
2. 风控上限按 session 单独维护
3. 标的权限通过 symbol 维度显式声明
4. `Tier A` 和 `Tier B` 的扩展时段能力不同
5. `POST` 默认禁用被动入场
6. `PRE / POST` 阈值高于 `CORE`

## 15. 回放、评估与上线

### 15.1 回放分桶

所有 replay/backtest/report 至少按以下维度分桶：

- `PRE / OPEN / CORE / CLOSE / POST`
- `Tier A / Tier B`
- `long / short`

### 15.2 核心指标

至少观察：

- `100ms / 500ms / 1s / 3s / 5s` markout
- 净 `PnL/share`
- TP 成交率
- 被动单存活率
- 部分成交率
- 滑点与 payup 分布
- 拒单率
- `short inventory block` 频率

### 15.3 场景覆盖

重点覆盖：

- spoofing / 假厚单
- hidden liquidity / 冰山单
- 开盘跳空和新闻 spike
- stale quote / data gap
- post-market 薄盘口
- 做空库存临时失效
- 挂出 TP 后市场快速反转

### 15.4 上线顺序

建议顺序固定为：

1. `capture` 至少稳定采集两个完整周
2. `shadow` 完整观察各时段和各标的分桶表现
3. `live` 先以 `1-share canary` 上线
4. 只有在税费后、分桶后仍保持正期望时才逐步放量

## 16. 明确不作为 v1 主线的方向

以下方向可以研究，但不应该写成当前主策略的组成部分：

- pair spread / basket stat-arb
- 协整检验与 z-score 配对信号
- 双腿同步执行与原子配对平仓
- 独立 `fair value` 引擎
- 独立 `vol-target` 账户预算引擎
- `LOBCAST / DeepLOB` 硬门槛
- 分钟级趋势系统与秒级 microstructure 的“双策略切换”

这些能力每一项都意味着新的状态机、新的执行语义或新的风险聚合方式，应该作为单独项目推进，而不是混入当前主策略说明书。

## 17. 一句话结论

这套策略的核心不是“多策略拼盘”，而是：

`一个统一的微观结构主 alpha`  
`加上分时段、分层级、可执行的 IBKR 风控与扩展时段约束`  
`再用 higher_tf_regime 做增强，而不是改写策略本体`
