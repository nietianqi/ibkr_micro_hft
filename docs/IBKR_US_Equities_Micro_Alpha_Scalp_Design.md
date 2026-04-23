# IBKR 美股微观高频策略设计说明书

## 1. 策略概述

### 1.1 策略名称

`IBKR US Equities Micro-Alpha Scalp`

### 1.2 策略定位

本策略面向 `IBKR` 环境下的美股超短线交易，核心模式为：

- 入场偏 `taker`
- 止盈偏 `maker`
- 止损偏 `taker`
- 持仓周期为秒级到几十秒
- 目标利润以 `1~2 tick` 或数美分为主

策略不追求微秒级速度优势，而是依靠订单簿、逐笔成交和微观结构状态的共振，在未来几秒到十几秒的极短窗口内捕捉延续性 alpha。

### 1.3 核心思想

在市场质量可接受的前提下，当多个微观结构信号同时指向同一方向，且追价风险仍可控时，使用可成交限价单主动开仓；成交后立刻挂出更优价格的被动止盈单；若边际优势消失，则立即撤退。

---

## 2. 适用品种与边界

### 2.1 适用品种

优先交易以下高流动性美股及 ETF：

- `AAPL`
- `NVDA`
- `AMD`
- `TSLA`
- `MSFT`
- `AMZN`
- `META`
- `SPY`
- `QQQ`

### 2.2 标的选择原则

候选标的需满足：

- 买一卖一长期连续有量
- 买卖价差稳定，不长期处于宽价差状态
- 逐笔成交连续
- 主动打单后滑点可控
- 不容易出现“一打就空”的虚弱盘口

### 2.3 避开场景

以下场景默认不交易：

- 超小盘股、低价妖股
- 流动性差的冷门股票
- 新闻驱动导致的极端波动状态
- 买卖价差明显异常拉宽
- 短时成交中断或数据流异常

---

## 3. 策略目标

### 3.1 交易目标

- 捕捉未来 `1s ~ 15s` 左右的短时方向延续
- 单笔赚取 `1~2 tick` 或小额价格改善
- 控制平均持仓时间，降低库存暴露
- 在非共置、非极低延迟环境下寻找仍可实现的最短可盈利持有周期

### 3.2 非目标

本策略不以以下能力为前提：

- 微秒级速度抢跑
- 纯队列优势做市
- 跨交易所延迟套利
- 长持仓趋势预测

---

## 4. 数据与事件模型

### 4.1 输入数据

策略需要至少包括以下实时数据流：

- `top-of-book` 或多档 `LOB`
- 实时逐笔成交
- 最新买一卖一与中间价
- 时间戳、序列号、数据新鲜度标记

### 4.2 事件归一化

所有行情更新归一为统一事件流：

- `BookUpdate`
- `TradePrint`
- `QuoteUpdate`
- `StatusUpdate`

统一字段建议：

- `ts_exchange`
- `ts_local`
- `symbol`
- `bid_px[i]`
- `bid_sz[i]`
- `ask_px[i]`
- `ask_sz[i]`
- `last_px`
- `last_sz`
- `trade_side`
- `is_stale`

### 4.3 时间窗口

建议同时维护多组短窗口，用于信号与风控：

- 事件窗口：最近 `N` 个事件
- 时间窗口：最近 `250ms / 500ms / 1s / 3s / 5s`
- 持仓窗口：入场后累计持仓时间

---

## 5. 核心信号定义

### 5.1 加权盘口不平衡

对前 `N` 档盘口按距离最优价的远近加权：

`B_w(t) = sum_{i=1..N}(w_i * bid_sz_i(t))`

`A_w(t) = sum_{i=1..N}(w_i * ask_sz_i(t))`

`Imbalance_w(t) = (B_w(t) - A_w(t)) / (B_w(t) + A_w(t) + eps)`

建议权重：

- `w_i = 1 / i`
- 或 `w_i = exp(-lambda * (i - 1))`

解释：

- `Imbalance_w > 0` 偏多
- `Imbalance_w < 0` 偏空

### 5.2 LOB-OFI

用于衡量订单簿变化中的主动供需变化。实盘里可先采用一档近似版本，再逐步扩展到多档。

对每个事件 `t`，定义一档 OFI 增量：

`e_bid(t) = 1_{bid_px(t) >= bid_px(t-1)} * bid_sz(t) - 1_{bid_px(t) <= bid_px(t-1)} * bid_sz(t-1)`

`e_ask(t) = 1_{ask_px(t) <= ask_px(t-1)} * ask_sz(t) - 1_{ask_px(t) >= ask_px(t-1)} * ask_sz(t-1)`

`OFI_lob(t, W) = sum_{tau in W}(e_bid(tau) - e_ask(tau))`

解释：

- `OFI_lob > 0` 代表买方补单更强或卖方撤单更多
- `OFI_lob < 0` 代表卖方补单更强或买方撤单更多

### 5.3 Tape-OFI

基于逐笔成交计算主动买卖量差：

`TapeOFI(t, W) = AggBuyVol(t, W) - AggSellVol(t, W)`

若需跨标的对比，可使用标准化版本：

`TapeOFI_z(t, W) = zscore(TapeOFI(t, W))`

解释：

- `TapeOFI > 0` 偏多
- `TapeOFI < 0` 偏空

### 5.4 Mid 与 Microprice

定义：

`Mid(t) = (bid_px_1(t) + ask_px_1(t)) / 2`

`Microprice(t) = (ask_px_1(t) * bid_sz_1(t) + bid_px_1(t) * ask_sz_1(t)) / (bid_sz_1(t) + ask_sz_1(t) + eps)`

### 5.5 Microprice Tilt

用于衡量盘口重心偏移：

`Tilt_mp(t) = (Microprice(t) - Mid(t)) / TickSize`

解释：

- `Tilt_mp > 0` 偏多
- `Tilt_mp < 0` 偏空

### 5.6 Microprice Momentum

可用短窗口斜率或差分表示：

`Mom_mp(t) = EMA(Microprice(t) - Microprice(t - Delta), alpha)`

或：

`Mom_mp(t) = slope(Microprice over last K events)`

解释：

- `Mom_mp > 0` 代表重心持续抬升
- `Mom_mp < 0` 代表重心持续下压

### 5.7 信号标准化

不同标的、不同时间段的信号尺度不同，建议每个标的独立维护标准化器：

- 滚动均值与标准差
- `EWMA` 波动估计
- 分位数映射

建议输出：

- 原始值
- `z-score`
- 离散方向值 `{-1, 0, +1}`

---

## 6. 辅助过滤器

### 6.1 市场质量过滤

仅在以下条件均满足时允许开仓：

- `spread_ticks <= max_spread_ticks`
- `bid_sz_1 >= min_top_depth`
- `ask_sz_1 >= min_top_depth`
- `trade_count(last X sec) >= min_trade_rate`
- `ts_local - ts_exchange <= max_data_age_ms`
- 短时波动不超过异常阈值

可选补充：

- 多档累计深度最低要求
- 最近若干事件内 quote 不得大幅跳空
- 开盘和重大新闻窗口内单独阈值

### 6.2 市场联动过滤

用于提高信号置信度：

- 大盘联动：`SPY`、`QQQ`
- 行业联动：`SMH`、`SOXX`、`XLK`、`XLF`
- 龙头联动：例如做 `AMD` 时观察 `NVDA`

简单规则：

- 若个股做多信号出现，同时联动标的微观方向不弱，则允许放行
- 若个股信号与板块/龙头显著背离，则降低分数或直接过滤

### 6.3 过热追单过滤

避免在 alpha 已经被兑现后再去追价：

- 最近 `M` 笔成交的累计推进不得超过 `max_chase_ticks`
- 下单价格距离当前最优价不得超过 `max_payup_ticks`
- 条件期望 `markout` 必须保持为正

---

## 7. 交易决策逻辑

### 7.1 评分框架

对多空分别建立分数：

`Score_long = a1 * Z(Imbalance_w) + a2 * Z(OFI_lob) + a3 * Z(TapeOFI) + a4 * Z(Mom_mp) + a5 * Z(Tilt_mp) + a6 * ContextFilter`

`Score_short = -Score_long`

其中：

- `a1~a5` 为主信号权重
- `a6` 为联动或环境增强项
- `ContextFilter` 可取 `{-1, 0, +1}` 或连续值

### 7.2 开仓条件

做多开仓示例：

- 市场质量过滤通过
- `Score_long >= long_entry_threshold`
- 至少 `k` 个主信号同向为正
- 过热追单过滤通过
- 当前无同方向持仓
- 全局与单票风控允许

做空对称。

### 7.3 不交易条件

以下任一成立则不新开仓：

- 市场状态异常
- 当前点差过宽
- 数据延迟或不新鲜
- 已触发策略冷却
- 当前已有过多未完成订单
- 单票或组合风险超限

---

## 8. 执行设计

### 8.1 入场方式

开仓使用 `marketable limit`，不用裸 `market`：

- 做多：价格设在 `ask_1` 或 `ask_1 + payup_limit`
- 做空：价格设在 `bid_1` 或 `bid_1 - payup_limit`

目的：

- 保证可成交性
- 限制最差成交价
- 控制极端滑点

### 8.2 成交后挂止盈

成交后立即挂出被动止盈单：

- 做多：`take_profit = entry_px + tp_ticks * TickSize`
- 做空：`take_profit = entry_px - tp_ticks * TickSize`

其中：

- 常规 `tp_ticks = 1`
- 当流强、延续性更好时可提升至 `2`

### 8.3 止损与撤退

若以下任一条件出现，立即主动平仓：

- `OFI_lob` 方向反转
- `TapeOFI` 方向反转
- `Mom_mp` 转负或转正失败
- 最优价失守
- 期望 `markout` 不再为正
- 持仓时间超过上限
- 出现异常跳价或流动性消失

### 8.4 撤单与重挂

执行层需支持：

- 成交后自动挂 `TP`
- 市场状态恶化时撤掉未成交 `TP`
- 持仓退出时联动撤销残余订单
- 限制改单频率，避免过度刷单

---

## 9. 风控设计

### 9.1 单笔风控

每笔交易均需满足：

- 固定美分止损上限
- 或 `1~2 tick` 失效止损
- 或 edge 反转即离场

建议止损以“信号失效 + 价格不利变化”联合判断，而不是只依赖固定价格。

### 9.2 持仓时间止损

若入场后在规定窗口内未按预期运行，直接退出：

- `max_hold_sec` 超时离场
- `min_progress_ticks` 未达成则提前放弃

### 9.3 连亏熔断

建议至少包括：

- 连续亏损 `N` 笔暂停
- 单票连续亏损暂停
- 某时间窗口累计亏损超过阈值暂停

### 9.4 市场状态熔断

出现以下情况停止新开仓：

- 点差显著异常
- quote 跳空频发
- 成交断流
- 波动超出日内分位阈值
- 数据质量异常或时钟漂移异常

### 9.5 仓位管理

限制项建议包括：

- 单笔最大股数
- 单票最大名义敞口
- 同板块总暴露限制
- 同时持仓数量限制
- 同时挂单数量限制

---

## 10. 资金管理

### 10.1 单笔仓位公式

可采用风险约束式仓位：

`size_raw = risk_budget_per_trade / max(stop_cents, spread_cents, vol_floor_cents)`

`size_depth_cap = depth_participation_rate * min(top_bid_depth, top_ask_depth)`

`size_final = min(size_raw, size_depth_cap, max_shares_per_order, symbol_max_shares)`

其中：

- `risk_budget_per_trade` 为单笔可承受亏损
- `vol_floor_cents` 代表短期波动底线
- `depth_participation_rate` 避免单次打穿盘口

### 10.2 仓位调整原则

- 流动性越好，可适度放大
- 点差越宽，仓位越小
- 波动越大，仓位越小
- 信号越强，可小幅放大
- 开盘、收盘、新闻窗口统一降档

---

## 11. 参数表

下表为初始研究参数，不代表实盘定值。

| 参数 | 含义 | 初始建议 |
|---|---|---|
| `book_levels` | 使用盘口档数 | `3~5` |
| `imbalance_lambda` | 盘口加权衰减 | `0.4~1.0` |
| `ofi_window_ms` | LOB-OFI 窗口 | `250~1000ms` |
| `tape_window_ms` | Tape-OFI 窗口 | `250~1000ms` |
| `mp_mom_window_ms` | Microprice Momentum 窗口 | `200~800ms` |
| `max_spread_ticks` | 允许最大点差 | `1~2 tick` |
| `min_top_depth` | 最小买一卖一深度 | 按标的分层 |
| `min_trade_rate` | 最低成交频率 | 按标的分层 |
| `long_entry_threshold` | 做多分数阈值 | 回测标定 |
| `short_entry_threshold` | 做空分数阈值 | 回测标定 |
| `min_signal_agree` | 同向信号最小数 | `3/5` 或 `4/5` |
| `max_chase_ticks` | 过热追价限制 | `1 tick` |
| `max_payup_ticks` | 入场最差加价 | `0~1 tick` |
| `tp_ticks` | 止盈档位 | `1`，强流时 `2` |
| `max_hold_sec` | 最长持仓时间 | `3~20s` |
| `cooldown_sec` | 出场后冷却 | `0~5s` |
| `max_loss_streak` | 连亏暂停阈值 | `3~5` |
| `symbol_daily_loss` | 单票日内最大亏损 | 自定义 |
| `strategy_daily_loss` | 策略日内最大亏损 | 自定义 |

---

## 12. 伪代码

```text
on_event(event):
    update_market_state(event)
    if not market_state.is_fresh:
        return

    update_book_features()
    update_trade_features()
    update_microprice_features()
    update_linkage_features()

    if has_position(symbol):
        manage_open_position(symbol)
        return

    if not pass_market_quality_filter(symbol):
        return

    long_score  = calc_long_score(symbol)
    short_score = calc_short_score(symbol)

    if is_overheated(symbol):
        return

    if risk_manager.block_new_entry(symbol):
        return

    if long_score >= LONG_THRESHOLD and long_signal_agreement(symbol) >= K:
        px = calc_marketable_limit_buy(symbol)
        qty = calc_position_size(symbol)
        send_buy_order(symbol, qty, px)
        return

    if short_score >= SHORT_THRESHOLD and short_signal_agreement(symbol) >= K:
        px = calc_marketable_limit_sell(symbol)
        qty = calc_position_size(symbol)
        send_sell_order(symbol, qty, px)
        return

on_fill(fill):
    register_position(fill)
    place_take_profit_order(fill.symbol, fill.qty, calc_tp_price(fill))

manage_open_position(symbol):
    if edge_has_reversed(symbol):
        cancel_take_profit_if_needed(symbol)
        flatten_aggressively(symbol)
        return

    if holding_time(symbol) > MAX_HOLD_SEC:
        cancel_take_profit_if_needed(symbol)
        flatten_aggressively(symbol)
        return

    if market_state_has_degraded(symbol):
        cancel_take_profit_if_needed(symbol)
        flatten_aggressively(symbol)
        return
```

---

## 13. 回测设计

### 13.1 回测原则

回测必须尽量贴近执行现实，不能只看信号方向正确率。

最低要求：

- 使用事件驱动回放
- 区分下单、确认、成交、撤单延迟
- 使用可成交限价单逻辑
- 对未成交、部分成交、滑点进行模拟
- 考虑佣金、交易费用、可能的路由差异

### 13.2 关键评估指标

#### 收益类

- 总收益
- 净收益
- 每股收益 `PnL / share`
- 每笔期望收益
- 胜率
- 盈亏比

#### 执行类

- 下单到成交延迟
- 成交率
- 部分成交率
- 平均滑点
- 追价后 adverse selection

#### 微观结构类

- `markout` 曲线：`100ms / 500ms / 1s / 3s / 5s / 10s`
- 入场前后 OFI 演变
- 入场后 microprice 演变
- 被动止盈成交率

#### 风险类

- 最大回撤
- 日内回撤
- 连亏分布
- 单票亏损贡献
- 时段分布风险

### 13.3 样本切分

建议至少做：

- 标的分层验证
- 不同时段验证
- 不同波动 regime 验证
- 训练、验证、留出测试切分

---

## 14. 实盘监控项

### 14.1 行情质量

- 数据延迟
- 数据缺包率
- 行情时间戳与本地时间偏差
- stale quote 比例

### 14.2 信号健康

- 各信号均值、方差、分位数
- 信号触发频率
- 多空触发比例
- `score` 分布漂移

### 14.3 执行健康

- 下单拒绝率
- 撤单失败率
- 成交率
- 平均滑点
- 止盈单成交率
- 退出延迟

### 14.4 交易表现

- 每分钟成交笔数
- 每股净收益
- 实时 `markout`
- 单票贡献
- 当前连亏状态

### 14.5 Kill Switch

满足以下任一条件立即切到保护模式：

- 数据流明显异常
- 下单异常或回报异常
- 当日亏损超限
- 连亏超限
- 平均滑点显著偏离历史分位

---

## 15. 模块化实现建议

建议拆为以下六个模块：

### 15.1 行情归一化模块

职责：

- 接收并统一 `book / trade / quote / status`
- 维护按标的隔离的最新状态
- 对脏数据、缺包、陈旧数据打标

### 15.2 信号计算模块

职责：

- 计算 `imbalance`
- 计算 `LOB-OFI`
- 计算 `Tape-OFI`
- 计算 `microprice / momentum / tilt`
- 输出标准化后的信号快照

### 15.3 市场质量过滤模块

职责：

- `spread / depth / trade rate / stale`
- 过热追价检测
- 联动环境确认

### 15.4 策略决策模块

职责：

- 汇总信号得分
- 生成开平仓意图
- 管理冷却、超时、边际优势失效

### 15.5 执行模块

职责：

- 发送 `marketable limit`
- 成交后自动挂止盈
- 退出时主动平仓
- 维护订单状态机

### 15.6 风控模块

职责：

- 单笔、单票、组合风险控制
- 连亏与日内熔断
- 市场异常保护

---

## 16. 研究与落地顺序

建议按以下顺序推进：

1. 先实现事件归一化与回放框架。
2. 实现五个主信号与标准化输出。
3. 用无执行摩擦的研究回测验证信号方向性。
4. 加入市场质量过滤和过热过滤。
5. 接入执行仿真，验证 `taker entry + maker exit` 的净收益。
6. 再加入仓位、熔断、组合层风控。
7. 最后接实盘监控和告警。

---

## 17. 关键结论

这套策略的本质不是预测日内大方向，而是在 `IBKR` 这种非共置环境下，寻找仍然具有正期望的最短持有周期。

它的可行性依赖三点：

- 信号足够确认
- 成交足够克制
- 退出足够果断

如果后续进入代码实现阶段，第一优先级不是做复杂模型，而是先把以下基础设施做好：

- 统一事件流
- 低误差信号计算
- 真实执行仿真
- 强约束风控
