"""
策略核心逻辑单元测试：仓位计算、保证金校验、订单成交
"""
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import StrategyConfig
from strategy import StrategyEngine
from models import StrategyState, OrderSide, OrderStatus


def make_engine(**kwargs) -> StrategyEngine:
    """创建默认引擎，支持覆盖任意配置"""
    defaults = dict(
        initial_capital=10000.0,
        leverage=5,
        min_trade_opportunities=100,
        max_position_ratio=1.0,
        grid_spacing_coeff=0.2,
        first_order_coeff=0.2,
        timeframe_minutes=1,
        min_qty_step=0.001,
        price_tick=0.1,
        contract_value=1.0,
    )
    defaults.update(kwargs)
    return StrategyEngine(StrategyConfig(**defaults))


class TestCalcQty:
    def test_returns_positive(self):
        engine = make_engine()
        qty = engine._calc_qty(close=50000.0, atr=200.0, capital=10000.0)
        assert qty > 0

    def test_atr_zero_returns_zero(self):
        engine = make_engine()
        qty = engine._calc_qty(close=50000.0, atr=0.0, capital=10000.0)
        assert qty == 0.0

    def test_capital_zero_returns_zero(self):
        engine = make_engine()
        qty = engine._calc_qty(close=50000.0, atr=200.0, capital=0.0)
        assert qty == 0.0

    def test_qty_aligned_to_min_step(self):
        engine = make_engine(min_qty_step=0.001)
        qty = engine._calc_qty(close=50000.0, atr=200.0, capital=10000.0)
        # qty 应该是 min_qty_step 的整数倍
        assert abs(round(qty / 0.001) * 0.001 - qty) < 1e-9

    def test_higher_leverage_smaller_qty(self):
        """杠杆越高，lev_adj 越小，qty 越小"""
        engine5  = make_engine(leverage=5)
        engine20 = make_engine(leverage=20)
        qty5  = engine5._calc_qty(close=50000.0, atr=200.0, capital=10000.0)
        qty20 = engine20._calc_qty(close=50000.0, atr=200.0, capital=10000.0)
        assert qty5 >= qty20

    def test_longer_timeframe_larger_qty(self):
        """周期越长，tf_adj 越大，qty 越大"""
        engine1m  = make_engine(timeframe_minutes=1)
        engine1h  = make_engine(timeframe_minutes=60)
        qty1m = engine1m._calc_qty(close=50000.0, atr=200.0, capital=10000.0)
        qty1h = engine1h._calc_qty(close=50000.0, atr=200.0, capital=10000.0)
        assert qty1h >= qty1m


class TestValidateMargin:
    def test_no_existing_positions(self):
        """无持仓时，保证金校验不应缩减 qty"""
        engine = make_engine(leverage=10, max_position_ratio=1.0)
        # 6单 × qty × close / leverage <= capital
        close, capital = 50000.0, 10000.0
        qty = 0.001  # 极小仓位，保证足够
        result = engine._validate_margin(qty, close, capital)
        assert result == qty

    def test_exceeds_margin_reduces_qty(self):
        """超出保证金上限时应缩减 qty"""
        engine = make_engine(leverage=1, max_position_ratio=0.1)
        close, capital = 50000.0, 10000.0
        # 这个 qty 肯定超过 10% 本金保证金
        qty = 1.0
        result = engine._validate_margin(qty, close, capital)
        assert result < qty


class TestFillOrder:
    def test_fill_creates_position(self):
        engine = make_engine()
        from models import Order, OrderSide, OrderStatus
        order = Order(
            order_id="test-001",
            side=OrderSide.BUY,
            price=50000.0,
            qty=0.01,
            placed_bar=0,
        )
        engine.pending_orders.append(order)
        engine.fill_order(order, fill_price=49900.0, bar_idx=1)

        assert order.status == OrderStatus.FILLED
        assert len(engine.open_positions) == 1
        pos = engine.open_positions[0]
        assert pos.entry_price == 49900.0
        assert pos.qty == 0.01

    def test_fill_deducts_margin(self):
        engine = make_engine(leverage=10)
        from models import Order, OrderSide
        order = Order(
            order_id="test-002",
            side=OrderSide.BUY,
            price=50000.0,
            qty=0.01,
            placed_bar=0,
        )
        initial_balance = engine.wallet_balance
        engine.fill_order(order, fill_price=50000.0, bar_idx=1)
        expected_margin = 0.01 * 50000.0 / 10
        assert abs(engine.wallet_balance - (initial_balance - expected_margin)) < 1e-6

    def test_equity_unchanged_after_fill(self):
        """成交后权益（含保证金）应保持不变（手续费除外）"""
        engine = make_engine(leverage=10, commission_rate=0.0)
        from models import Order, OrderSide
        order = Order(
            order_id="test-003",
            side=OrderSide.BUY,
            price=50000.0,
            qty=0.01,
            placed_bar=0,
        )
        equity_before = engine.equity(50000.0)
        engine.fill_order(order, fill_price=50000.0, bar_idx=1)
        equity_after = engine.equity(50000.0)
        assert abs(equity_after - equity_before) < 1e-6


class TestClosePositions:
    def test_close_long_profit(self):
        """多单盈利平仓后 wallet_balance 应增加"""
        engine = make_engine(leverage=10, commission_rate=0.0, slippage_rate=0.0)
        from models import Order, OrderSide
        order = Order(order_id="t", side=OrderSide.BUY, price=50000.0, qty=0.1, placed_bar=0)
        engine.fill_order(order, fill_price=50000.0, bar_idx=0)
        balance_after_open = engine.wallet_balance

        engine._close_all_positions(close=51000.0, bar_idx=1, reason="TAKE_PROFIT")
        assert engine.wallet_balance > balance_after_open + 50000.0 * 0.1 / 10  # 归还保证金+盈利

    def test_close_clears_positions(self):
        engine = make_engine(leverage=10, commission_rate=0.0, slippage_rate=0.0)
        from models import Order, OrderSide
        order = Order(order_id="t", side=OrderSide.BUY, price=50000.0, qty=0.1, placed_bar=0)
        engine.fill_order(order, fill_price=50000.0, bar_idx=0)
        engine._close_all_positions(close=50000.0, bar_idx=1, reason="TAKE_PROFIT")
        assert len(engine.open_positions) == 0

    def test_close_creates_trade(self):
        engine = make_engine(leverage=10, commission_rate=0.0, slippage_rate=0.0)
        from models import Order, OrderSide
        order = Order(order_id="t", side=OrderSide.BUY, price=50000.0, qty=0.1, placed_bar=0)
        engine.fill_order(order, fill_price=50000.0, bar_idx=0)
        engine._close_all_positions(close=50000.0, bar_idx=1, reason="TAKE_PROFIT")
        assert len(engine.trades) == 1
        assert engine.trades[0].exit_reason == "TAKE_PROFIT"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
