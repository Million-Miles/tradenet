"""
回测引擎 — 动态网格交易策略 V2.1

执行流程（每根K线）：
  1. 检查未成交限价单是否在本K线区间内成交
  2. 调用 StrategyEngine.on_bar() 执行策略逻辑
  3. 记录权益曲线数据点
"""
from __future__ import annotations

import math
import os
from typing import Optional

import pandas as pd
import numpy as np

from config import StrategyConfig
from indicators import precompute_indicators, indicators_ready
from models import Order, OrderSide, OrderStatus, StrategyState
from strategy import StrategyEngine


class BacktestEngine:
    """
    回测引擎：逐K线驱动策略，模拟限价单成交、滑点、手续费。
    """

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.engine = StrategyEngine(cfg)

    # ─────────────────────────────────────────────────────────────
    # 主入口
    # ─────────────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame) -> "BacktestResult":
        """
        输入 OHLCV DataFrame（列名：open/high/low/close/volume），
        运行完整回测，返回 BacktestResult。
        """
        # 预计算指标
        df = precompute_indicators(
            df,
            rsi_period=self.cfg.rsi_period,
            atr_period=self.cfg.atr_period,
            atr_spike_lookback=self.cfg.atr_spike_lookback,
        )
        df = df.reset_index(drop=False)  # 保留原始 index 作为 timestamp

        equity_curve = []

        for i, row in df.iterrows():
            if not indicators_ready(row):
                eq = self.engine.equity(row["close"])
                equity_curve.append({
                    "bar_idx": i,
                    "timestamp": row.get("timestamp", row.name),
                    "close": row["close"],
                    "equity": eq,
                    "wallet_balance": self.engine.wallet_balance,
                    "open_positions": len(self.engine.open_positions),
                    "pending_orders": len(self.engine.pending_orders),
                })
                continue

            # ── Step1: 检查限价单成交 ──────────────────────────
            self._check_fills(i, row["open"], row["high"], row["low"], row["close"])

            # ── Step2: 策略逻辑（K线收盘） ─────────────────────
            prev_rsi = df.loc[i - 1, "rsi"] if i > 0 else row["rsi"]
            if pd.isna(prev_rsi):
                prev_rsi = row["rsi"]

            log = self.engine.on_bar(
                bar_idx=i,
                timestamp=row.get("timestamp", row.name),
                open_=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                rsi=row["rsi"],
                prev_rsi=float(prev_rsi),
                atr=row["atr"],
                atr_mean=row["atr_mean"],
            )

            # ── Step3: 记录权益 ───────────────────────────────
            eq = self.engine.equity(row["close"])
            equity_curve.append({
                "bar_idx": i,
                "timestamp": row.get("timestamp", row.name),
                "close": row["close"],
                "equity": eq,
                "wallet_balance": self.engine.wallet_balance,
                "open_positions": len(self.engine.open_positions),
                "pending_orders": len(self.engine.pending_orders),
            })

        return BacktestResult(
            cfg=self.cfg,
            engine=self.engine,
            equity_curve=pd.DataFrame(equity_curve),
        )

    # ─────────────────────────────────────────────────────────────
    # 限价单成交检查
    # ─────────────────────────────────────────────────────────────

    def _check_fills(
        self,
        bar_idx: int,
        open_: float,
        high: float,
        low: float,
        close: float,
    ) -> None:
        """
        按本K线的价格区间检查所有 PENDING 订单是否成交。

        成交规则：
          - 买入限价单（BUY）：bar.low <= order.price → 成交
          - 卖出限价单（SELL）：bar.high >= order.price → 成交
          - 跳空处理：若 open 已穿越挂单价，以 open 价成交（保守模拟）
          - 成交价格：取 min(order.price, open_) 对买单，反之对卖单

        按价格顺序处理（最近的先成交）以正确积累保证金。
        """
        pending = [o for o in self.engine.pending_orders if o.status == OrderStatus.PENDING]
        if not pending:
            return

        # 多单买入：从高到低（最近的价格先成交）
        buy_orders = sorted(
            [o for o in pending if o.side == OrderSide.BUY],
            key=lambda o: o.price, reverse=True
        )
        # 空单卖出：从低到高
        sell_orders = sorted(
            [o for o in pending if o.side == OrderSide.SELL],
            key=lambda o: o.price
        )

        for order in buy_orders:
            if low <= order.price:
                # 跳空（gap down）：开盘价低于挂单价，以开盘价成交
                fill_price = min(order.price, open_)
                self.engine.fill_order(order, fill_price, bar_idx)

        for order in sell_orders:
            if high >= order.price:
                # 跳空（gap up）：开盘价高于挂单价，以开盘价成交
                fill_price = max(order.price, open_)
                self.engine.fill_order(order, fill_price, bar_idx)

        # 清理已成交订单
        self.engine.pending_orders = [
            o for o in self.engine.pending_orders
            if o.status == OrderStatus.PENDING
        ]


# ─────────────────────────────────────────────────────────────────
# 回测结果 & 统计报告
# ─────────────────────────────────────────────────────────────────

class BacktestResult:
    def __init__(
        self,
        cfg: StrategyConfig,
        engine: StrategyEngine,
        equity_curve: pd.DataFrame,
    ):
        self.cfg = cfg
        self.engine = engine
        self.equity_curve = equity_curve
        self._stats: Optional[dict] = None

    @property
    def trades(self):
        return self.engine.trades

    @property
    def bar_logs(self):
        return self.engine.bar_logs

    def stats(self) -> dict:
        """计算并缓存回测统计指标"""
        if self._stats is not None:
            return self._stats

        trades = self.trades
        eq = self.equity_curve["equity"]
        initial = self.cfg.initial_capital

        if not trades:
            self._stats = {"error": "无任何成交记录"}
            return self._stats

        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        final_equity = float(eq.iloc[-1])
        total_return_pct = (final_equity - initial) / initial * 100

        # 最大回撤
        running_max = eq.cummax()
        drawdown = (eq - running_max) / running_max
        max_drawdown_pct = float(drawdown.min()) * 100

        # 夏普比率（假设0无风险收益，按K线周期）
        equity_returns = eq.pct_change().dropna()
        if equity_returns.std() > 0:
            sharpe = (equity_returns.mean() / equity_returns.std()) * math.sqrt(
                365 * 24 * 60 / self.cfg.timeframe_minutes
            )
        else:
            sharpe = 0.0

        # 盈亏比
        avg_win  = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses else float("inf")

        self._stats = {
            "symbol":           self.cfg.symbol,
            "timeframe":        self.cfg.timeframe,
            "initial_capital":  initial,
            "final_equity":     round(final_equity, 2),
            "total_pnl":        round(final_equity - initial, 2),
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio":     round(sharpe, 3),
            "total_trades":     len(trades),
            "win_trades":       len(wins),
            "loss_trades":      len(losses),
            "win_rate_pct":     round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "avg_win":          round(avg_win, 2),
            "avg_loss":         round(avg_loss, 2),
            "profit_factor":    round(profit_factor, 3) if profit_factor != float("inf") else None,
            "total_commission": round(sum(t.commission for t in trades), 2),
            "final_state":      self.engine.state.value,
        }
        return self._stats

    def print_report(self) -> None:
        """打印格式化的回测报告"""
        s = self.stats()
        line = "─" * 52
        print(f"\n{'═' * 52}")
        print(f"  动态网格交易策略 V2.1 — 回测报告")
        print(f"{'═' * 52}")
        print(f"  标的:        {s.get('symbol')}  {s.get('timeframe')}")
        print(f"  初始本金:    {s.get('initial_capital'):,.2f} USDT")
        print(f"  最终权益:    {s.get('final_equity'):,.2f} USDT")
        print(f"  总收益:      {s.get('total_pnl'):+,.2f} USDT  ({s.get('total_return_pct'):+.2f}%)")
        print(line)
        print(f"  最大回撤:    {s.get('max_drawdown_pct'):.2f}%")
        print(f"  夏普比率:    {s.get('sharpe_ratio'):.3f}")
        print(line)
        print(f"  总交易次数:  {s.get('total_trades')}")
        print(f"  盈利次数:    {s.get('win_trades')}  亏损次数: {s.get('loss_trades')}")
        print(f"  胜率:        {s.get('win_rate_pct'):.1f}%")
        print(f"  平均盈利:    {s.get('avg_win'):+.2f} USDT")
        print(f"  平均亏损:    {s.get('avg_loss'):+.2f} USDT")
        pf = s.get('profit_factor')
        print(f"  盈亏比:      {'∞（无亏损交易）' if pf is None else f'{pf:.3f}'}")
        print(line)
        print(f"  累计手续费:  {s.get('total_commission'):,.2f} USDT")
        print(f"  策略最终状态: {s.get('final_state')}")
        print(f"{'═' * 52}\n")

    def to_trades_df(self) -> pd.DataFrame:
        """返回所有成交记录的 DataFrame"""
        if not self.trades:
            return pd.DataFrame()
        rows = []
        for t in self.trades:
            rows.append({
                "trade_id":    t.trade_id,
                "side":        t.side.value,
                "entry_bar":   t.entry_bar,
                "exit_bar":    t.exit_bar,
                "entry_price": t.entry_price,
                "exit_price":  t.exit_price,
                "qty":         t.qty,
                "pnl":         round(t.pnl, 4),
                "commission":  round(t.commission, 4),
                "exit_reason": t.exit_reason,
            })
        return pd.DataFrame(rows)

    def print_event_log(self) -> None:
        """打印关键事件：成交后总持仓量+均价、止盈/止损盈亏（🟢/🔴）"""
        from collections import defaultdict

        # bar_idx → timestamp 映射（从权益曲线取）
        ts_map: dict = {}
        for _, row in self.equity_curve.iterrows():
            ts_map[int(row["bar_idx"])] = str(row["timestamp"])[:16]

        # 从 bar_logs 提取 PLACE_ORDERS 事件：place_bar → 信号标签
        SIGNAL_LABELS = {
            "LONG_SECOND_A":  "背离(多)",
            "LONG_SECOND_B":  "深度超卖",
            "SHORT_SECOND_A": "背离(空)",
            "SHORT_SECOND_B": "深度超买",
        }
        place_events: list = []  # [(place_bar, label)] 有序列表
        for log in self.bar_logs:
            if log.action.startswith("PLACE_ORDERS"):
                label = SIGNAL_LABELS.get(log.signal, log.signal)
                place_events.append((log.bar_idx, label))

        def get_signal_label(fill_bar: int) -> str:
            """找 fill_bar 之前最近的一次 PLACE_ORDERS 信号"""
            result = ""
            for pb, lbl in place_events:
                if pb <= fill_bar:
                    result = lbl
                else:
                    break
            return result

        # 按 entry_bar 整理成交（新建仓）
        fills_by_bar: dict = defaultdict(list)
        for t in self.trades:
            fills_by_bar[t.entry_bar].append(t)

        # 按 exit_bar 整理平仓
        exits_by_bar: dict = defaultdict(list)
        for t in self.trades:
            exits_by_bar[t.exit_bar].append(t)

        # 合并所有事件 bar，按时间顺序处理
        all_bars = sorted(set(list(fills_by_bar.keys()) + list(exits_by_bar.keys())))

        print(f"\n{'═'*68}")
        print(f"  交易事件日志")
        print(f"{'═'*68}")

        open_pos: list = []  # (entry_price, qty)
        current_signal = ""  # 本轮信号标签

        for bar in all_bars:
            ts = ts_map.get(bar, f"bar{bar}")

            # ── 成交事件（先于平仓，同一bar填完再平）────────────
            filled = fills_by_bar.get(bar, [])
            if filled:
                sig = get_signal_label(bar)
                if sig:
                    current_signal = sig
                for t in filled:
                    open_pos.append((t.entry_price, t.qty))
                total_qty = sum(p[1] for p in open_pos)
                avg_price = sum(p[0] * p[1] for p in open_pos) / total_qty if total_qty else 0
                side = filled[0].side.value
                sig_str = f"  [{current_signal}]" if current_signal else ""
                print(f"[{ts}] 成交{len(filled)}单  "
                      f"持仓{len(open_pos)}单  总qty={total_qty:.4f}  均价={avg_price:.1f}  {side}{sig_str}")

            # ── 平仓事件 ─────────────────────────────────────────
            exited = exits_by_bar.get(bar, [])
            if exited:
                total_pnl  = sum(t.pnl for t in exited)
                total_comm = sum(t.commission for t in exited)
                exit_price = exited[0].exit_price
                reason     = exited[0].exit_reason
                label = {"TAKE_PROFIT": "止盈",
                         "SINGLE_STOP": "单次止损",
                         "GLOBAL_STOP": "全局止损"}.get(reason, reason)
                icon = "🔴" if reason in ("SINGLE_STOP", "GLOBAL_STOP") else "🟢"
                sign = "+" if total_pnl >= 0 else ""
                print(f"[{ts}] {icon} {label}  平{len(exited)}单  "
                      f"exit={exit_price:.1f}  "
                      f"PnL={sign}{total_pnl:.2f}  手续费={total_comm:.2f}")
                open_pos.clear()
                current_signal = ""

        print(f"{'═'*68}\n")

    def to_bar_logs_df(self) -> pd.DataFrame:
        """返回每根K线的策略日志 DataFrame"""
        if not self.bar_logs:
            return pd.DataFrame()
        rows = []
        for b in self.bar_logs:
            rows.append({
                "bar_idx":        b.bar_idx,
                "timestamp":      b.timestamp,
                "close":          b.close,
                "rsi":            b.rsi,
                "atr":            b.atr,
                "state":          b.state,
                "action":         b.action,
                "equity":         b.equity,
                "wallet_balance": b.wallet_balance,
                "open_positions": b.open_positions,
                "pending_orders": b.pending_orders,
                "note":           b.note,
            })
        return pd.DataFrame(rows)
