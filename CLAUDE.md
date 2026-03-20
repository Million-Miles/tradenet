# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 行为规范

### 1. 规划节点默认规则
- 所有非简单任务（含3步以上操作或架构决策），必须先进入规划模式
- 若执行出现偏差，立即停止并重新规划，不得强行推进
- 规划模式需覆盖验证环节，而非仅用于功能构建
- 提前编写详细的需求规格，减少歧义

### 2. 子代理策略
- 灵活大量使用子代理，保持主对话上下文窗口整洁
- 将调研、探索、并行分析类工作分流给子代理执行
- 针对复杂问题，通过子代理投入更多算力解决
- 单个子代理仅负责一项任务，保证执行聚焦

### 3. 自我优化循环
- 收到用户的任何修正后，将对应模式更新至 `tasks/lessons.md`
- 为自身制定规则，杜绝重复犯错
- 启动项目会话时，先回顾 `tasks/lessons.md` 中的历史教训

### 4. 完成前强制验证
- 未证明功能可正常运行前，绝对不能标记任务完成
- 自我审视：「资深工程师会认可这份交付吗？」
- 运行测试、检查日志、证明代码正确性

### 5. 追求优雅
- 针对非简单改动，暂停并反问「有没有更优雅的实现方式？」
- 若修复方案过于粗糙临时，重写为优雅的解决方案
- 简单、明确的修复可跳过此环节，避免过度设计

### 6. 自动化缺陷修复
- 收到缺陷报告后直接修复，无需额外指导
- 定位日志、报错信息、未通过的测试用例，完成问题修复

---

## 任务管理规范
1. **优先规划**：将带可核对检查项的执行计划写入 `tasks/todo.md`
2. **计划校验**：启动开发前，先确认计划无误
3. **进度追踪**：随执行进度同步标记完成项
4. **结果归档**：在 `tasks/todo.md` 中补充结果复盘章节
5. **教训沉淀**：收到修正后，同步更新 `tasks/lessons.md`

---

## 核心原则
- **简洁优先**：所有改动尽可能精简，实现最小代码侵入
- **杜绝敷衍**：定位问题根因，不做临时修复，始终遵循资深工程师开发标准

---

## 运行回测

```bash
python3 run_backtest.py
# 自定义配置文件
python3 run_backtest.py --config config.ini --backtest backtest.ini
```

K线数据缓存在 `kdata/` 目录（`{symbol}_{interval}_{YYYY-MM}.csv`），已下载的月份不会重复下载。

## 配置文件

- **`config.ini`** — 策略参数（用户调参）：RSI周期、网格间距、止盈止损、杠杆等
- **`backtest.ini`** — 回测参数：数据来源、资金、手续费、合约精度、输出路径

## 代码架构

### 数据流

```
run_backtest.py
  → fetch_binance_klines() / load_csv()   # 加载K线数据
  → BacktestEngine.run()                  # 逐K线驱动
      → precompute_indicators()           # 预计算 Wilder RSI + ATR
      → _check_fills()                    # 检查限价单成交（先于策略逻辑）
      → StrategyEngine.on_bar()           # 8步策略逻辑
  → BacktestResult.print_report()
  → BacktestResult.print_event_log()      # 交易事件日志（含🟢/🔴颜色）
```

### 核心模块职责

- **`config.py`** — `StrategyConfig` dataclass，`from_ini()` 同时读取两个 ini 文件
- **`models.py`** — 枚举和数据类：`StrategyState`, `SignalType`, `Order`, `Position`, `Trade`, `RSISignalState`, `OversoldEpisode/OverboughtEpisode`
- **`indicators.py`** — Wilder RSI 和 ATR（EWM alpha=1/period，与 TradingView 一致）
- **`strategy.py`** — `StrategyEngine`，8步每K线执行逻辑
- **`backtest.py`** — `BacktestEngine`（逐K线驱动）+ `BacktestResult`（统计报告）

### 策略状态机

`IDLE` ↔ `LONG_GRID` / `SHORT_GRID` → `STOPPED`

### 开单信号（V2.2，仅两种）

无首次信号，必须有前次超卖/超买记录：
- **深度超卖/超买（B）**：前次RSI极值超过 `divergence_depth`（默认20/80），不判断背离
- **底背离/顶背离（A）**：前次RSI极值在 20~30（或70~80），需价格与RSI方向相反，且当前价已从极值点回头

### 关键设计细节

**手续费会计**：开仓只锁定保证金（`wallet_balance -= margin`），手续费存在 `Position.entry_commission`，平仓时统一结算。`equity = wallet_balance + used_margin + unrealized_pnl`

**限价单成交**：买单 `bar.low ≤ order.price`，卖单 `bar.high ≥ order.price`；跳空时以 `bar.open` 成交

**仓位计算**：
```
qty_base = capital × max_position_ratio / (N_min × 6 × ATR)
lev_adj  = clip(5/leverage, 0.1, 1.0)
tf_adj   = clip(√tf_minutes/√60, 0.2, 1.0)
qty      = floor(qty_base × lev_adj × tf_adj / min_step) × min_step
```

**止盈**：持仓总浮盈 ≥ `take_profit_usdt`（USDT 固定金额，0=不启用）

**挂单停止**：`LONG_GRID` 时 RSI > 30 撤单；`SHORT_GRID` 时 RSI < 70 撤单；无持仓则回到 IDLE

---

## 历史教训

> 详细持续更新的教训记录见 `tasks/lessons.md`，以下为项目初期已修复的关键 bug。

**止盈在亏损时触发**：曾用 RSI 超买/超卖回头作为止盈条件，未判断浮盈正负，导致亏损时也触发止盈。现已改为 `take_profit_usdt` 固定金额，且只在 `upnl > 0` 时触发。

**状态机卡死**：ORDER_STOP 撤单后若有持仓，状态保持 `LONG/SHORT_GRID` 不变，导致策略永远不回 IDLE、无法开新单。修复：ORDER_STOP 后无持仓时必须调用 `_reset_round_state()` 并将状态设回 `IDLE`。

**回测报告 total_pnl 不对**：曾用所有成交的 `trade.pnl` 求和，正确做法：`total_pnl = final_equity - initial_capital`。

**事件日志时间戳显示为整数**：误用 `row["index"]` 而非 `row["timestamp"]`，应统一用 `row.get("timestamp", row.name)`。

**网格成交率极低**：斐波那契间距 `[0,1,2,3,5,8]` × `spacing_coeff=1.0` ATR，第6单距现价约 8.5 ATR（1m图≈325 USDT），几乎不会成交。1m图建议 `spacing_coeff=0.2`。

**FIRST 信号导致趋势行情大亏**：首次超卖/超买直接开单，在单边趋势中连续触发，累积大量反向仓位。已删除 FIRST 信号，仅保留二次信号（背离 A + 深度 B）。
