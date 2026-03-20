# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
