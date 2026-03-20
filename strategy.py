"""
策略核心逻辑 — 动态网格交易策略 V2.1
每根K线收盘后执行完整的8步流程。
"""
from __future__ import annotations

import math
import uuid
from typing import List, Optional, Tuple

from config import StrategyConfig, RSI_OVERBOUGHT, RSI_OVERSOLD
from models import (
    StrategyState, OrderSide, OrderStatus, SignalType,
    RSISignalState, Order, Position, Trade, BarLog,
)


class StrategyEngine:
    """
    策略引擎：维护状态机，按K线驱动执行。
    回测引擎每根K线调用 on_bar()，传入已成交的订单更新后再调用。
    """

    def __init__(self, cfg: StrategyConfig):
        cfg.validate()
        self.cfg = cfg

        # ── 账户状态 ──────────────────────────────────────────────
        self.wallet_balance: float = cfg.initial_capital   # 已实现权益
        self.initial_capital: float = cfg.initial_capital

        # ── 持仓 & 挂单 ──────────────────────────────────────────
        self.open_positions: List[Position] = []
        self.pending_orders: List[Order]    = []

        # ── 已平仓交易记录 ────────────────────────────────────────
        self.trades: List[Trade] = []

        # ── 策略状态 ──────────────────────────────────────────────
        self.state: StrategyState = StrategyState.IDLE

        # ── RSI信号状态 ───────────────────────────────────────────
        self.signal_state: RSISignalState = RSISignalState()

        # ── 每根K线的日志 ─────────────────────────────────────────
        self.bar_logs: List[BarLog] = []

        # ── 内部计数器 ────────────────────────────────────────────
        self._order_counter: int = 0
        self._position_counter: int = 0
        self._trade_counter: int = 0

    # ─────────────────────────────────────────────────────────────
    # 公共属性
    # ─────────────────────────────────────────────────────────────

    def total_unrealized_pnl(self, current_price: float) -> float:
        return sum(p.unrealized_pnl(current_price) for p in self.open_positions)

    def used_margin(self) -> float:
        return sum(p.margin for p in self.open_positions)

    def equity(self, current_price: float) -> float:
        # wallet_balance 中已扣除锁定保证金，需加回：
        # equity = 可用资金 + 锁定保证金 + 未实现盈亏
        return self.wallet_balance + self.used_margin() + self.total_unrealized_pnl(current_price)

    def current_capital(self, current_price: float) -> float:
        """当前本金 = 当前权益（每轮开单前动态更新）"""
        return self.equity(current_price)

    # ─────────────────────────────────────────────────────────────
    # 主入口：每根K线收盘后调用
    # ─────────────────────────────────────────────────────────────

    def on_bar(
        self,
        bar_idx: int,
        timestamp,
        open_: float,
        high: float,
        low: float,
        close: float,
        rsi: float,
        prev_rsi: float,
        atr: float,
        atr_mean: float,
    ) -> BarLog:
        """
        执行完整的每根K线8步逻辑，返回本根K线的日志。
        注意：limit订单的成交由 backtest.py 在调用此函数之前处理。
        """
        cfg = self.cfg
        action = ""
        note_parts = []
        signal = SignalType.NONE

        # ── 更新RSI episode状态（信号判断前）────────────────────
        self.signal_state.on_bar_oversold_tracking(
            bar_idx, rsi, close, RSI_OVERBOUGHT, RSI_OVERSOLD
        )
        self.signal_state.on_bar_overbought_tracking(
            bar_idx, rsi, close, RSI_OVERBOUGHT, RSI_OVERSOLD
        )

        # ══ Step1: 指标已在调用前计算（由回测引擎传入） ══════════

        # ══ Step2: 全局止损检查（最高优先级） ═══════════════════
        capital = self.current_capital(close)
        cumulative_loss = self.initial_capital - capital  # 正值=已亏损
        if cumulative_loss > self.initial_capital * cfg.global_stop_loss:
            self._close_all_positions(close, bar_idx, "GLOBAL_STOP")
            self._cancel_all_orders()
            self.state = StrategyState.STOPPED
            action = "GLOBAL_STOP"
            note_parts.append(
                f"全局止损触发: 累计亏损={cumulative_loss:.2f} > "
                f"{self.initial_capital * cfg.global_stop_loss:.2f}"
            )
            return self._make_log(bar_idx, timestamp, close, rsi, atr, action, capital, note_parts)

        # ══ Step3: 止盈检查 ══════════════════════════════════════
        if self.state in (StrategyState.LONG_GRID, StrategyState.SHORT_GRID):
            if self.open_positions:
                upnl = self.total_unrealized_pnl(close)
                tp_usdt = cfg.take_profit_usdt > 0 and upnl >= cfg.take_profit_usdt
                tp_pct  = cfg.take_profit_pct  > 0 and upnl >= capital * cfg.take_profit_pct
                if tp_usdt or tp_pct:
                    self._close_all_positions(close, bar_idx, "TAKE_PROFIT")
                    self._cancel_all_orders()
                    self._reset_round_state()
                    self.state = StrategyState.IDLE
                    action = "TAKE_PROFIT"
                    reason = f"{cfg.take_profit_usdt:.2f} USDT" if tp_usdt else f"{cfg.take_profit_pct*100:.1f}% 本金"
                    note_parts.append(f"止盈: 浮盈={upnl:.2f} >= {reason}")
                    return self._make_log(bar_idx, timestamp, close, rsi, atr, action, capital, note_parts)

        # ══ Step4: 单次策略止损检查 ══════════════════════════════
        if self.state in (StrategyState.LONG_GRID, StrategyState.SHORT_GRID):
            upnl = self.total_unrealized_pnl(close)
            if upnl < 0 and abs(upnl) > capital * cfg.single_strategy_stop_loss:
                self._close_all_positions(close, bar_idx, "SINGLE_STOP")
                self._cancel_all_orders()
                self._reset_round_state()
                self.state = StrategyState.IDLE
                action = "SINGLE_STOP"
                note_parts.append(
                    f"单次策略止损: 浮亏={upnl:.2f} > {capital * cfg.single_strategy_stop_loss:.2f}"
                )
                return self._make_log(bar_idx, timestamp, close, rsi, atr, action, capital, note_parts)

        # ══ Step5: ATR突变保护 ══════════════════════════════════
        atr_spike = (atr > atr_mean * cfg.atr_spike_multiplier)
        if atr_spike:
            self._cancel_all_orders()
            action = "ATR_SPIKE"
            note_parts.append(f"ATR突变保护: ATR={atr:.4f} > 均值×{cfg.atr_spike_multiplier}")
            return self._make_log(bar_idx, timestamp, close, rsi, atr, action, capital, note_parts)

        # ══ Step6: 挂单停止条件检查 ══════════════════════════════
        if self.state == StrategyState.LONG_GRID:
            if rsi > RSI_OVERSOLD:  # 离开超卖区域（RSI > 30）
                self._cancel_all_orders()
                action = "ORDER_STOP"
                note_parts.append(f"多单挂单停止: RSI={rsi:.1f}>{RSI_OVERSOLD}")
                if not self.open_positions:
                    self._reset_round_state()
                    self.state = StrategyState.IDLE
                return self._make_log(bar_idx, timestamp, close, rsi, atr, action, capital, note_parts)

        elif self.state == StrategyState.SHORT_GRID:
            if rsi < RSI_OVERBOUGHT:  # 离开超买区域（RSI < 70）
                self._cancel_all_orders()
                action = "ORDER_STOP"
                note_parts.append(f"空单挂单停止: RSI={rsi:.1f}<{RSI_OVERBOUGHT}")
                if not self.open_positions:
                    self._reset_round_state()
                    self.state = StrategyState.IDLE
                return self._make_log(bar_idx, timestamp, close, rsi, atr, action, capital, note_parts)

        # ══ Step7: RSI开单信号判断 ═══════════════════════════════
        # 持仓不阻断同方向挂单：多单状态允许继续挂多，空单状态允许继续挂空
        if self.state == StrategyState.STOPPED:
            signal = SignalType.NONE
        else:
            signal = self._detect_signal(bar_idx, rsi, prev_rsi, close)
            long_signals = (SignalType.LONG_SECOND_A, SignalType.LONG_SECOND_B)
            short_signals = (SignalType.SHORT_SECOND_A, SignalType.SHORT_SECOND_B)
            if self.state == StrategyState.LONG_GRID and signal not in long_signals:
                signal = SignalType.NONE
            elif self.state == StrategyState.SHORT_GRID and signal not in short_signals:
                signal = SignalType.NONE

        # ══ Step8: 执行挂单 ══════════════════════════════════════
        if signal != SignalType.NONE:
            self._cancel_all_orders()
            new_orders = self._build_grid_orders(bar_idx, signal, close, atr, capital)
            if new_orders:
                self.pending_orders.extend(new_orders)
                # 更新状态
                if signal in (SignalType.LONG_SECOND_A, SignalType.LONG_SECOND_B):
                    self.state = StrategyState.LONG_GRID
                else:
                    self.state = StrategyState.SHORT_GRID
                action = f"PLACE_ORDERS ({signal.value})"
                note_parts.append(f"挂{len(new_orders)}单 close={close:.2f} ATR={atr:.4f}")

        log = self._make_log(bar_idx, timestamp, close, rsi, atr, action, capital, note_parts, signal)
        self.bar_logs.append(log)
        return log

    # ─────────────────────────────────────────────────────────────
    # Step7 — RSI信号检测
    # ─────────────────────────────────────────────────────────────

    def _detect_signal(
        self,
        bar_idx: int,
        rsi: float,
        prev_rsi: float,
        close: float,
    ) -> SignalType:
        """判断当前K线是否触发多单或空单信号，返回 SignalType。"""
        cfg = self.cfg
        ss = self.signal_state

        # ── 多单信号（超卖区间内RSI回头）────────────────────────
        if rsi < RSI_OVERSOLD and rsi > prev_rsi:
            return self._check_episode_signal(
                bar_idx=bar_idx,
                close=close,
                rsi=rsi,
                prev_episode=ss.prev_oversold,
                curr_episode=ss.curr_oversold,
                bars_since=ss.bars_since_prev_oversold(bar_idx),
                had_opposite=ss.between_oversold_had_overbought,
                is_long=True,
            )

        # ── 空单信号（超买区间内RSI回头）────────────────────────
        if rsi > RSI_OVERBOUGHT and rsi < prev_rsi:
            return self._check_episode_signal(
                bar_idx=bar_idx,
                close=close,
                rsi=rsi,
                prev_episode=ss.prev_overbought,
                curr_episode=ss.curr_overbought,
                bars_since=ss.bars_since_prev_overbought(bar_idx),
                had_opposite=ss.between_overbought_had_oversold,
                is_long=False,
            )

        return SignalType.NONE

    def _check_episode_signal(
        self,
        bar_idx: int,
        close: float,
        rsi: float,
        prev_episode,
        curr_episode,
        bars_since,
        had_opposite: bool,
        is_long: bool,
    ) -> SignalType:
        """通用信号判断：多空对称逻辑，避免重复代码。"""
        cfg = self.cfg

        if prev_episode is None:
            return SignalType.NONE
        if bars_since is not None and bars_since > cfg.second_signal_valid_bars:
            return SignalType.NONE
        if had_opposite:
            return SignalType.NONE

        signal_b = SignalType.LONG_SECOND_B if is_long else SignalType.SHORT_SECOND_B
        signal_a = SignalType.LONG_SECOND_A if is_long else SignalType.SHORT_SECOND_A
        depth = cfg.rsi_divergence_depth

        if is_long:
            extreme_rsi  = prev_episode.rsi_low
            curr_extreme  = curr_episode.rsi_low   if curr_episode is not None else rsi
            curr_price    = curr_episode.price_low  if curr_episode is not None else close
            prev_price    = prev_episode.close_at_rsi_low
            deep_trigger  = extreme_rsi < depth
            normal_range  = depth <= extreme_rsi <= RSI_OVERSOLD
            divergence    = (curr_price < prev_price and curr_extreme > extreme_rsi and close > curr_price)
        else:
            extreme_rsi  = prev_episode.rsi_high
            curr_extreme  = curr_episode.rsi_high   if curr_episode is not None else rsi
            curr_price    = curr_episode.price_high if curr_episode is not None else close
            prev_price    = prev_episode.close_at_rsi_high
            deep_threshold = 100.0 - depth
            deep_trigger  = extreme_rsi > deep_threshold
            normal_range  = RSI_OVERBOUGHT <= extreme_rsi <= deep_threshold
            divergence    = (curr_price > prev_price and curr_extreme < extreme_rsi and close < curr_price)

        if deep_trigger:
            return signal_b
        if normal_range and divergence:
            return signal_a
        return SignalType.NONE

    # ─────────────────────────────────────────────────────────────
    # Step8 — 挂单位置 & 仓位计算
    # ─────────────────────────────────────────────────────────────

    def _build_grid_orders(
        self,
        bar_idx: int,
        signal: SignalType,
        close: float,
        atr: float,
        capital: float,
    ) -> List[Order]:
        """计算6个网格挂单，返回 Order 列表（已完成保证金校验）"""
        cfg = self.cfg
        is_long = signal in (SignalType.LONG_SECOND_A, SignalType.LONG_SECOND_B)
        side = OrderSide.BUY if is_long else OrderSide.SELL

        # ── 挂单价格（斐波那契间距：0,1,2,3,5,8）─────────────────
        F = cfg.first_order_coeff
        G = cfg.grid_spacing_coeff
        first_offset = F * atr
        spacings = [0, 1, 2, 3, 5, 8]  # 各单相对于first_order的间距倍数

        prices = []
        for s in spacings:
            if is_long:
                p = close - first_offset - s * G * atr
            else:
                p = close + first_offset + s * G * atr
            # 对齐价格精度
            p = self._round_price(p)
            prices.append(p)

        # ── 单笔仓位计算 ─────────────────────────────────────────
        qty = self._calc_qty(close, atr, capital)

        # ── 保证金校验 & 等比缩减 ─────────────────────────────────
        qty = self._validate_margin(qty, close, capital)
        if qty <= 0:
            return []

        # ── 构建订单 ─────────────────────────────────────────────
        orders = []
        for p in prices:
            if p <= 0:
                continue
            orders.append(Order(
                order_id=self._next_order_id(),
                side=side,
                price=p,
                qty=qty,
                placed_bar=bar_idx,
            ))
        return orders

    def _calc_qty(self, close: float, atr: float, capital: float) -> float:
        """
        单笔挂单数量计算（V2.1新公式）：
        确保全局止损触发前至少有 N_min 次完整网格开仓机会。

        qty_base = capital × max_position_ratio / (N_min × 6 × ATR)
        lev_adj  = clip(5 / leverage, 0.1, 1.0)
        tf_adj   = clip(√tf_minutes / √60, 0.2, 1.0)
        qty      = floor(qty_base × lev_adj × tf_adj / min_step) × min_step
        """
        if atr <= 0 or capital <= 0:
            return 0.0

        cfg = self.cfg
        n_min = cfg.min_trade_opportunities

        # 基础张数：保证 N_min 次机会（最坏情况6单全中×1ATR亏损≤总预算）
        qty_base = capital * cfg.max_position_ratio / (n_min * 6.0 * atr * cfg.contract_value)

        # 杠杆修正系数：杠杆越高，每张保证金越少，适当减小张数
        lev_adj = float(max(0.1, min(1.0, 5.0 / cfg.leverage)))

        # 周期修正系数：周期越短，ATR绝对值越小，适当减小张数
        tf_adj = float(max(0.2, min(1.0,
            math.sqrt(cfg.timeframe_minutes) / math.sqrt(60)
        )))

        raw_qty = qty_base * lev_adj * tf_adj

        # 向下取整到最小步长
        return self._floor_qty(raw_qty)

    def _validate_margin(self, qty: float, close: float, capital: float) -> float:
        """
        校验保证金占用，若超过最大持仓比例则等比缩减。
        6单总保证金 + 现有已用保证金 <= 当前本金 × 最大持仓比例
        """
        cfg = self.cfg
        used = self.used_margin()
        max_margin = capital * cfg.max_position_ratio

        single_margin = qty * close / cfg.leverage
        total_new_margin = single_margin * 6

        if total_new_margin + used > max_margin:
            available = max(0.0, max_margin - used)
            if available <= 0:
                return 0.0
            # 等比缩减：6单均等
            single_margin_max = available / 6.0
            qty = single_margin_max * cfg.leverage / close
            qty = self._floor_qty(qty)

        return qty

    # ─────────────────────────────────────────────────────────────
    # 平仓 & 清理辅助方法
    # ─────────────────────────────────────────────────────────────

    def _close_all_positions(
        self, close: float, bar_idx: int, reason: str
    ) -> None:
        """市价平所有持仓（含滑点和手续费）"""
        cfg = self.cfg
        slippage_factor = 1 - cfg.slippage_rate  # 多单平仓卖出，价格略低
        for pos in self.open_positions:
            if pos.side == OrderSide.BUY:
                exit_price = close * slippage_factor
                raw_pnl = (exit_price - pos.entry_price) * pos.qty
            else:
                exit_price = close * (2 - slippage_factor)  # 空单平仓买入，价格略高
                raw_pnl = (pos.entry_price - exit_price) * pos.qty

            exit_commission = exit_price * pos.qty * cfg.commission_rate
            total_commission = pos.entry_commission + exit_commission
            net_pnl = raw_pnl - total_commission  # 含入场+出场手续费

            # 归还保证金 + 结算盈亏（exit_commission已含在net_pnl中）
            self.wallet_balance += pos.margin + net_pnl

            self.trades.append(Trade(
                trade_id=self._next_trade_id(),
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                qty=pos.qty,
                entry_bar=pos.entry_bar,
                exit_bar=bar_idx,
                pnl=net_pnl,
                commission=total_commission,
                exit_reason=reason,
            ))

        self.open_positions.clear()

    def _cancel_all_orders(self) -> None:
        """撤销所有未成交挂单"""
        for o in self.pending_orders:
            if o.status == OrderStatus.PENDING:
                o.status = OrderStatus.CANCELLED
        self.pending_orders = [
            o for o in self.pending_orders if o.status == OrderStatus.PENDING
        ]

    def _reset_round_state(self) -> None:
        pass  # 预留扩展

    # ─────────────────────────────────────────────────────────────
    # 回测引擎调用：处理限价单成交
    # ─────────────────────────────────────────────────────────────

    def fill_order(self, order: Order, fill_price: float, bar_idx: int) -> None:
        """
        由回测引擎调用：确认某个挂单成交，创建持仓，更新余额。
        """
        cfg = self.cfg
        if order.status != OrderStatus.PENDING:
            return

        order.status = OrderStatus.FILLED
        order.filled_price = fill_price
        order.filled_bar = bar_idx

        margin = order.qty * fill_price / cfg.leverage
        entry_commission = fill_price * order.qty * cfg.commission_rate

        # 开仓只锁定保证金（手续费记录在持仓上，平仓时统一结算）
        self.wallet_balance -= margin

        pos = Position(
            position_id=self._next_position_id(),
            side=order.side,
            qty=order.qty,
            entry_price=fill_price,
            entry_bar=bar_idx,
            margin=margin,
            entry_commission=entry_commission,
        )
        self.open_positions.append(pos)

    # ─────────────────────────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────────────────────────

    def _round_price(self, price: float) -> float:
        tick = self.cfg.price_tick
        return round(round(price / tick) * tick, 8)

    def _floor_qty(self, qty: float) -> float:
        step = self.cfg.min_qty_step
        return math.floor(qty / step) * step

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"ORD-{self._order_counter:06d}"

    def _next_position_id(self) -> str:
        self._position_counter += 1
        return f"POS-{self._position_counter:06d}"

    def _next_trade_id(self) -> str:
        self._trade_counter += 1
        return f"TRD-{self._trade_counter:06d}"

    def _make_log(
        self,
        bar_idx: int,
        timestamp,
        close: float,
        rsi: float,
        atr: float,
        action: str,
        capital: float,
        note_parts: list,
        signal: SignalType = SignalType.NONE,
    ) -> BarLog:
        return BarLog(
            bar_idx=bar_idx,
            timestamp=timestamp,
            close=close,
            rsi=round(rsi, 2),
            atr=round(atr, 4),
            state=self.state.value,
            signal=signal.value,
            action=action,
            equity=round(capital, 4),
            wallet_balance=round(self.wallet_balance, 4),
            open_positions=len(self.open_positions),
            pending_orders=len(self.pending_orders),
            note="; ".join(note_parts),
        )
