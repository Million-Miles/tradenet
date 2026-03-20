"""
数据模型 — 动态网格交易策略 V2.1
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────
# 枚举
# ─────────────────────────────────────────────────────────────────

class StrategyState(Enum):
    IDLE        = "IDLE"
    LONG_GRID   = "LONG_GRID"
    SHORT_GRID  = "SHORT_GRID"
    STOPPED     = "STOPPED"


class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING   = "PENDING"
    FILLED    = "FILLED"
    CANCELLED = "CANCELLED"


class StopThreshold(Enum):
    """挂单停止条件阈值类型"""
    NORMAL   = "NORMAL"    # 多单RSI>30 / 空单RSI<70
    EXTENDED = "EXTENDED"  # 多单RSI>40 / 空单RSI<60


class SignalType(Enum):
    NONE            = "NONE"
    LONG_FIRST      = "LONG_FIRST"       # 首次超卖
    LONG_SECOND_A   = "LONG_SECOND_A"    # 二次超卖A（普通背离）
    LONG_SECOND_B   = "LONG_SECOND_B"    # 二次超卖B（深度超卖）
    SHORT_FIRST     = "SHORT_FIRST"
    SHORT_SECOND_A  = "SHORT_SECOND_A"
    SHORT_SECOND_B  = "SHORT_SECOND_B"


# ─────────────────────────────────────────────────────────────────
# 超卖 / 超买 episode（一次连续超卖/超买区间的记录）
# ─────────────────────────────────────────────────────────────────

@dataclass
class OversoldEpisode:
    """记录一次完整的 RSI 超卖区间"""
    entry_bar: int               # 进入超卖的K线索引
    rsi_low: float               # 区间内RSI最低值
    bar_at_rsi_low: int          # RSI最低值对应的K线索引
    close_at_rsi_low: float      # RSI最低值对应的收盘价
    price_low: float             # 区间内收盘价最低值（用于背离判断）
    exit_bar: Optional[int] = None  # 离开超卖的K线索引（None=区间仍在进行）


@dataclass
class OverboughtEpisode:
    """记录一次完整的 RSI 超买区间"""
    entry_bar: int
    rsi_high: float
    bar_at_rsi_high: int
    close_at_rsi_high: float
    price_high: float            # 区间内收盘价最高值（用于背离判断）
    exit_bar: Optional[int] = None


# ─────────────────────────────────────────────────────────────────
# RSI 信号状态跟踪
# ─────────────────────────────────────────────────────────────────

@dataclass
class RSISignalState:
    """持续维护的RSI信号状态记录"""

    # ── 超卖跟踪（多单信号） ──────────────────────────────────
    prev_oversold: Optional[OversoldEpisode] = None   # 前次超卖episode
    curr_oversold: Optional[OversoldEpisode] = None   # 当前正在进行的超卖episode
    # 前次超卖结束后，至今是否出现过RSI > 70
    between_oversold_had_overbought: bool = False

    # ── 超买跟踪（空单信号） ──────────────────────────────────
    prev_overbought: Optional[OverboughtEpisode] = None
    curr_overbought: Optional[OverboughtEpisode] = None
    # 前次超买结束后，至今是否出现过RSI < 30
    between_overbought_had_oversold: bool = False

    def bars_since_prev_oversold(self, current_bar: int) -> Optional[int]:
        """前次超卖结束至今的K线数（前次超卖exit_bar → 当前bar）"""
        if self.prev_oversold is None or self.prev_oversold.exit_bar is None:
            return None
        return current_bar - self.prev_oversold.exit_bar

    def bars_since_prev_overbought(self, current_bar: int) -> Optional[int]:
        if self.prev_overbought is None or self.prev_overbought.exit_bar is None:
            return None
        return current_bar - self.prev_overbought.exit_bar

    def on_bar_oversold_tracking(
        self, bar_idx: int, rsi: float, close: float,
        rsi_overbought: float, rsi_oversold: float
    ) -> None:
        """每根K线更新超卖episode状态（在信号判断之前调用）"""
        if rsi < rsi_oversold:
            if self.curr_oversold is None:
                # 新的超卖episode开始
                self.curr_oversold = OversoldEpisode(
                    entry_bar=bar_idx,
                    rsi_low=rsi,
                    bar_at_rsi_low=bar_idx,
                    close_at_rsi_low=close,
                    price_low=close,
                )
            else:
                # 更新当前episode的最低RSI和最低价
                if rsi < self.curr_oversold.rsi_low:
                    self.curr_oversold.rsi_low = rsi
                    self.curr_oversold.bar_at_rsi_low = bar_idx
                    self.curr_oversold.close_at_rsi_low = close
                if close < self.curr_oversold.price_low:
                    self.curr_oversold.price_low = close
        else:
            if self.curr_oversold is not None:
                # 超卖episode结束 → 保存为prev
                self.curr_oversold.exit_bar = bar_idx
                self.prev_oversold = self.curr_oversold
                self.curr_oversold = None
                self.between_oversold_had_overbought = False  # 重置中间超买标记

        # 跟踪两次超卖之间是否触及RSI > 70
        if self.prev_oversold is not None and self.curr_oversold is None:
            if rsi > rsi_overbought:
                self.between_oversold_had_overbought = True

    def on_bar_overbought_tracking(
        self, bar_idx: int, rsi: float, close: float,
        rsi_overbought: float, rsi_oversold: float
    ) -> None:
        """每根K线更新超买episode状态（在信号判断之前调用）"""
        if rsi > rsi_overbought:
            if self.curr_overbought is None:
                self.curr_overbought = OverboughtEpisode(
                    entry_bar=bar_idx,
                    rsi_high=rsi,
                    bar_at_rsi_high=bar_idx,
                    close_at_rsi_high=close,
                    price_high=close,
                )
            else:
                if rsi > self.curr_overbought.rsi_high:
                    self.curr_overbought.rsi_high = rsi
                    self.curr_overbought.bar_at_rsi_high = bar_idx
                    self.curr_overbought.close_at_rsi_high = close
                if close > self.curr_overbought.price_high:
                    self.curr_overbought.price_high = close
        else:
            if self.curr_overbought is not None:
                self.curr_overbought.exit_bar = bar_idx
                self.prev_overbought = self.curr_overbought
                self.curr_overbought = None
                self.between_overbought_had_oversold = False

        # 跟踪两次超买之间是否触及RSI < 30
        if self.prev_overbought is not None and self.curr_overbought is None:
            if rsi < rsi_oversold:
                self.between_overbought_had_oversold = True


# ─────────────────────────────────────────────────────────────────
# 挂单 / 持仓 / 成交记录
# ─────────────────────────────────────────────────────────────────

@dataclass
class Order:
    order_id: str
    side: OrderSide
    price: float
    qty: float
    placed_bar: int
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_bar: Optional[int] = None


@dataclass
class Position:
    position_id: str
    side: OrderSide      # BUY=多单, SELL=空单
    qty: float
    entry_price: float
    entry_bar: int
    margin: float        # 占用保证金
    entry_commission: float = 0.0  # 入场手续费（已从wallet_balance扣除）

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == OrderSide.BUY:
            return (current_price - self.entry_price) * self.qty
        else:
            return (self.entry_price - current_price) * self.qty


@dataclass
class Trade:
    """一笔已平仓交易的完整记录"""
    trade_id: str
    side: OrderSide
    entry_price: float
    exit_price: float
    qty: float
    entry_bar: int
    exit_bar: int
    pnl: float               # 平仓盈亏（含手续费）
    commission: float
    exit_reason: str         # "TAKE_PROFIT" | "SINGLE_STOP" | "GLOBAL_STOP"


# ─────────────────────────────────────────────────────────────────
# 每根K线的策略日志（用于回测分析）
# ─────────────────────────────────────────────────────────────────

@dataclass
class BarLog:
    bar_idx: int
    timestamp: object
    close: float
    rsi: float
    atr: float
    state: str
    signal: str
    action: str              # "PLACE_ORDERS" | "CANCEL_ORDERS" | "STOP_LOSS" | "TAKE_PROFIT" | ""
    equity: float
    wallet_balance: float
    open_positions: int
    pending_orders: int
    note: str = ""
