"""
技术指标计算 — RSI（Wilder平滑）、ATR（Wilder平滑）
"""
import numpy as np
import pandas as pd
from typing import Tuple


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI（与TradingView / 币安K线指标一致）
    返回与 close 等长的 Series，前 period 个值为 NaN。
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # 初始化：前 period 根用简单均值作为种子
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    # avg_loss=0 且 avg_gain>0 时 RSI=100；avg_loss=0 且 avg_gain=0 时 RSI=50（横盘）
    rsi = pd.Series(np.where(
        avg_loss == 0,
        np.where(avg_gain == 0, 50.0, 100.0),
        100.0 - (100.0 / (1.0 + avg_gain / avg_loss)),
    ), index=close.index)
    rsi[avg_gain.isna() | avg_loss.isna()] = np.nan
    return rsi


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> pd.Series:
    """
    Wilder's ATR
    返回与 close 等长的 Series，前 period 个值为 NaN。
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr


def calc_atr_mean(atr: pd.Series, lookback: int = 20) -> pd.Series:
    """前 lookback 根ATR的简单移动均值（用于突变保护检查）"""
    return atr.rolling(window=lookback, min_periods=lookback).mean()


def precompute_indicators(
    df: pd.DataFrame,
    rsi_period: int = 14,
    atr_period: int = 14,
    atr_spike_lookback: int = 20,
) -> pd.DataFrame:
    """
    一次性计算所有指标，附加到 DataFrame 并返回。
    输入 df 必须含列: open, high, low, close, volume（小写）
    输出新增列: rsi, atr, atr_mean
    """
    df = df.copy()
    df["rsi"]      = calc_rsi(df["close"], period=rsi_period)
    df["atr"]      = calc_atr(df["high"], df["low"], df["close"], period=atr_period)
    df["atr_mean"] = calc_atr_mean(df["atr"], lookback=atr_spike_lookback)
    return df


def indicators_ready(row: pd.Series) -> bool:
    """判断当前K线所有指标是否已有效（非NaN）"""
    return (
        pd.notna(row["rsi"])
        and pd.notna(row["atr"])
        and pd.notna(row["atr_mean"])
    )
