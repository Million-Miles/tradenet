"""
指标计算单元测试：RSI 和 ATR
"""
import math
import pandas as pd
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from indicators import calc_rsi, calc_atr, precompute_indicators, indicators_ready


def make_ohlcv(closes, high_offset=1.0, low_offset=1.0):
    """构造简单 OHLCV DataFrame"""
    n = len(closes)
    return pd.DataFrame({
        "open":   closes,
        "high":   [c + high_offset for c in closes],
        "low":    [c - low_offset  for c in closes],
        "close":  closes,
        "volume": [1000.0] * n,
    })


class TestRSI:
    def test_rsi_length(self):
        closes = pd.Series(range(1, 31), dtype=float)
        rsi = calc_rsi(closes, period=14)
        assert len(rsi) == len(closes)

    def test_rsi_range(self):
        """RSI 值必须在 0~100 之间"""
        closes = pd.Series([float(i) for i in range(1, 51)])
        rsi = calc_rsi(closes, period=14).dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_rsi_all_up(self):
        """持续上涨时 RSI 应趋近 100"""
        closes = pd.Series([float(100 + i) for i in range(50)])
        rsi = calc_rsi(closes, period=14).dropna()
        assert rsi.iloc[-1] > 90

    def test_rsi_all_down(self):
        """持续下跌时 RSI 应趋近 0"""
        closes = pd.Series([float(100 - i) for i in range(50)])
        rsi = calc_rsi(closes, period=14).dropna()
        assert rsi.iloc[-1] < 10

    def test_rsi_nan_before_period(self):
        """前 period 根 K 线 RSI 应为 NaN"""
        closes = pd.Series(range(1, 20), dtype=float)
        rsi = calc_rsi(closes, period=14)
        assert rsi.iloc[:13].isna().all()


class TestATR:
    def test_atr_length(self):
        df = make_ohlcv([float(100 + i) for i in range(30)])
        atr = calc_atr(df["high"], df["low"], df["close"], period=14)
        assert len(atr) == len(df)

    def test_atr_positive(self):
        """ATR 值应为正数"""
        df = make_ohlcv([float(100 + i % 10) for i in range(30)])
        atr = calc_atr(df["high"], df["low"], df["close"], period=14).dropna()
        assert (atr > 0).all()

    def test_atr_flat_market(self):
        """完全横盘（high=low=close）时 ATR 应为 0"""
        n = 30
        df = pd.DataFrame({
            "open":  [100.0] * n,
            "high":  [100.0] * n,
            "low":   [100.0] * n,
            "close": [100.0] * n,
            "volume":[1000.0] * n,
        })
        atr = calc_atr(df["high"], df["low"], df["close"], period=14).dropna()
        assert (atr == 0).all()


class TestPrecompute:
    def test_precompute_adds_columns(self):
        df = make_ohlcv([float(100 + i % 20) for i in range(50)])
        result = precompute_indicators(df, rsi_period=14, atr_period=14, atr_spike_lookback=20)
        assert "rsi" in result.columns
        assert "atr" in result.columns
        assert "atr_mean" in result.columns

    def test_indicators_ready_false_before_warmup(self):
        df = make_ohlcv([float(100 + i % 20) for i in range(50)])
        result = precompute_indicators(df, rsi_period=14, atr_period=14, atr_spike_lookback=20)
        # 前几行应该 not ready
        assert not indicators_ready(result.iloc[0])

    def test_indicators_ready_true_after_warmup(self):
        df = make_ohlcv([float(100 + i % 20) for i in range(50)])
        result = precompute_indicators(df, rsi_period=14, atr_period=14, atr_spike_lookback=20)
        # 后面的行应该 ready
        assert indicators_ready(result.iloc[-1])


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
