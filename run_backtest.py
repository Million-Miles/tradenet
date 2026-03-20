"""
回测入口 — 动态网格交易策略 V2.1

用法：
  # 从CSV文件回测
  python run_backtest.py --csv data/BTCUSDT_1m.csv

  # 从币安API拉取历史数据回测
  python run_backtest.py --symbol BTCUSDT --interval 1m \
      --start 2024-01-01 --end 2024-06-01

  # 自定义参数
  python run_backtest.py --symbol BTCUSDT --interval 1m \
      --start 2024-01-01 --end 2024-06-01 \
      --capital 50000 --leverage 5 \
      --first-coeff 0.5 --spacing-coeff 1.0

CSV格式要求（无表头含义要求，但必须包含这些列名）：
  timestamp, open, high, low, close, volume
"""

import argparse
import sys
import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config import StrategyConfig, INTERVAL_MINUTES
from backtest import BacktestEngine


# ─────────────────────────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────────────────────────

def load_csv(path: str) -> pd.DataFrame:
    """从CSV加载OHLCV数据，标准化列名为小写"""
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]

    # 尝试解析时间列
    for col in ("timestamp", "time", "date", "datetime", "open_time"):
        if col in df.columns:
            try:
                df["timestamp"] = pd.to_datetime(df[col], unit="ms", errors="coerce")
                if df["timestamp"].isna().all():
                    df["timestamp"] = pd.to_datetime(df[col], errors="coerce")
            except Exception:
                df["timestamp"] = pd.to_datetime(df[col], errors="coerce")
            break

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV缺少必要列: {missing}")

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.reset_index(drop=True)
    print(f"[数据] 加载 {len(df)} 根K线，范围: {df.get('timestamp', pd.Series()).iloc[0]} "
          f"~ {df.get('timestamp', pd.Series()).iloc[-1]}")
    return df


def fetch_binance_klines(
    symbol: str,
    interval: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    """
    直接用 requests 调用币安合约 Klines REST API，支持代理。
    无需 ccxt，读取 HTTPS_PROXY / HTTP_PROXY 环境变量。
    """
    import os
    import time
    import requests

    URL = "https://fapi.binance.com/fapi/v1/klines"
    LIMIT = 1500  # 单次最大返回条数

    interval_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
        "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
        "1d": 86_400_000,
    }.get(interval, 60_000)

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    if proxy:
        print(f"[数据] 使用代理: {proxy}")

    start_ts = int(pd.Timestamp(start).timestamp() * 1000)
    end_ts   = int(pd.Timestamp(end).timestamp() * 1000)

    all_rows = []
    since = start_ts
    session = requests.Session()
    session.proxies.update(proxies)

    import io, zipfile
    CDN_BASE  = "https://data.binance.vision/data/futures/um/monthly/klines"
    KDATA_DIR = "kdata"
    os.makedirs(KDATA_DIR, exist_ok=True)

    COLS = ["timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"]

    months     = pd.period_range(pd.Timestamp(start).to_period("M"),
                                 pd.Timestamp(end).to_period("M"), freq="M")
    month_dfs  = []
    need_download = []

    # ── 检查本地缓存 ─────────────────────────────────────────────
    for month in months:
        cache_file = os.path.join(KDATA_DIR, f"{symbol}_{interval}_{month}.csv")
        if os.path.exists(cache_file):
            mdf = pd.read_csv(cache_file, parse_dates=["timestamp"])
            month_dfs.append((month, mdf))
            print(f"[数据] 读取缓存 {month} ({cache_file})")
        else:
            need_download.append(month)

    # ── 下载缺失月份 ─────────────────────────────────────────────
    if need_download:
        print(f"[数据] 需下载 {len(need_download)} 个月: {', '.join(str(m) for m in need_download)}")
        for month in need_download:
            cache_file = os.path.join(KDATA_DIR, f"{symbol}_{interval}_{month}.csv")
            zip_url = f"{CDN_BASE}/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"
            downloaded = False
            try:
                r = session.get(zip_url, timeout=60)
                if r.status_code == 404:
                    print(f"[数据] {month} 暂无数据（404），跳过")
                    continue
                r.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    with z.open(z.namelist()[0]) as f:
                        mdf = pd.read_csv(f, header=None)
                if pd.isna(pd.to_numeric(mdf.iloc[0, 0], errors="coerce")):
                    mdf = mdf.iloc[1:].reset_index(drop=True)
                mdf.columns = COLS
                for col in ("open", "high", "low", "close", "volume"):
                    mdf[col] = pd.to_numeric(mdf[col])
                mdf["timestamp"] = pd.to_datetime(mdf["timestamp"], unit="ms")
                mdf = mdf[["timestamp", "open", "high", "low", "close", "volume"]]
                mdf.to_csv(cache_file, index=False)
                month_dfs.append((month, mdf))
                print(f"[数据] 已下载并缓存 {month} ({len(mdf):,} 根K线) → {cache_file}")
                downloaded = True
            except Exception as e:
                print(f"[数据] CDN下载失败 ({month}): {e}")

            # CDN失败则回退到REST API
            if not downloaded:
                print(f"[数据] 回退至REST API下载 {month}...")
                m_start = int(pd.Timestamp(str(month.start_time)).timestamp() * 1000)
                m_end   = int(pd.Timestamp(str(month.end_time)).timestamp() * 1000)
                rows = []
                since_ts = m_start
                while since_ts < m_end:
                    params = {"symbol": symbol, "interval": interval,
                              "startTime": since_ts, "endTime": m_end, "limit": LIMIT}
                    resp = session.get(URL, params=params, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                    if not data:
                        break
                    rows.extend(data)
                    since_ts = data[-1][0] + interval_ms
                    if len(data) < LIMIT:
                        break
                    time.sleep(0.1)
                if rows:
                    mdf = pd.DataFrame(rows, columns=COLS)
                    for col in ("open", "high", "low", "close", "volume"):
                        mdf[col] = pd.to_numeric(mdf[col])
                    mdf["timestamp"] = pd.to_datetime(mdf["timestamp"], unit="ms")
                    mdf = mdf[["timestamp", "open", "high", "low", "close", "volume"]]
                    mdf.to_csv(cache_file, index=False)
                    month_dfs.append((month, mdf))
                    print(f"[数据] API下载完成 {month} ({len(mdf):,} 根K线) → {cache_file}")

    if not month_dfs:
        raise RuntimeError("未获取到任何K线数据，请检查交易对名称和时间范围")

    # ── 合并并裁剪到精确时间范围 ─────────────────────────────────
    month_dfs.sort(key=lambda x: x[0])
    df = pd.concat([mdf for _, mdf in month_dfs], ignore_index=True)
    df = df[(df["timestamp"] >= pd.Timestamp(start)) &
            (df["timestamp"] <= pd.Timestamp(end))]
    df = df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)

    print(f"[数据] 共 {len(df):,} 根K线，范围: {df['timestamp'].iloc[0]} ~ {df['timestamp'].iloc[-1]}")
    return df


# ─────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="动态网格交易策略 V2.1 — 回测工具",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--config",        type=str,   default="config.ini",  help="策略参数文件（默认 config.ini）")
    parser.add_argument("--backtest",       type=str,   default="backtest.ini", help="回测参数文件（默认 backtest.ini）")
    # 命令行覆盖参数（优先级高于 ini 文件）
    parser.add_argument("--capital",        type=float, default=None, help="初始本金 USDT")
    parser.add_argument("--leverage",       type=int,   default=None, help="杠杆倍数")
    parser.add_argument("--first-coeff",    type=float, default=None, help="第一单距现价系数")
    parser.add_argument("--spacing-coeff",  type=float, default=None, help="网格间距系数")
    parser.add_argument("--take-profit",    type=float, default=None, help="止盈金额 USDT（0=不启用）")
    parser.add_argument("--stop-loss",      type=float, default=None, help="单次策略止损比例（如 0.3）")
    args = parser.parse_args()

    # ── 加载配置 ─────────────────────────────────────────────────
    print(f"[配置] 策略参数: {args.config}")
    print(f"[配置] 回测参数: {args.backtest}")
    cfg = StrategyConfig.from_ini(args.config, args.backtest)

    # ── 命令行参数覆盖 ───────────────────────────────────────────
    if args.capital       is not None: cfg.initial_capital        = args.capital
    if args.leverage      is not None: cfg.leverage               = args.leverage
    if args.first_coeff   is not None: cfg.first_order_coeff      = args.first_coeff
    if args.spacing_coeff is not None: cfg.grid_spacing_coeff     = args.spacing_coeff
    if args.take_profit   is not None: cfg.take_profit_usdt       = args.take_profit
    if args.stop_loss     is not None: cfg.single_strategy_stop_loss = args.stop_loss
    cfg.validate()

    # ── 读取回测专用字段（数据来源、输出路径）────────────────────
    import configparser as _cp
    bt_ini = _cp.ConfigParser(inline_comment_prefixes=("#", ";"))
    bt_ini.read(args.backtest, encoding="utf-8")

    def bt_get(section, key, fallback=""):
        return bt_ini.get(section, key, fallback=str(fallback)).strip()

    data_source = bt_get("data", "source", "binance")

    # interval 从策略配置文件读取
    _s = _cp.ConfigParser(inline_comment_prefixes=("#", ";"))
    _s.read(args.config, encoding="utf-8")
    interval = _s.get("timeframe", "interval", fallback="1m")

    if data_source == "csv":
        csv_path = bt_get("data", "csv_path", "")
        if not csv_path:
            print("[错误] backtest.ini [data] source=csv 时必须填写 csv_path")
            sys.exit(1)
        df = load_csv(csv_path)
    else:
        symbol = bt_get("data", "symbol", "BTCUSDT")
        start  = bt_get("data", "start",  "")
        end    = bt_get("data", "end",    "")
        if not start or not end:
            print("[错误] backtest.ini [data] 必须填写 start 和 end")
            sys.exit(1)
        df = fetch_binance_klines(symbol, interval, start, end)

    # ── 运行回测 ─────────────────────────────────────────────────
    print(f"\n[回测] 开始运行... 共 {len(df)} 根K线")
    result = BacktestEngine(cfg).run(df)

    # ── 输出报告 ─────────────────────────────────────────────────
    result.print_report()
    result.print_event_log()

    # ── 保存输出文件 ─────────────────────────────────────────────
    out_trades = bt_get("output", "trades", "")
    out_equity = bt_get("output", "equity", "")
    out_logs   = bt_get("output", "logs",   "")

    if out_trades:
        trades_df = result.to_trades_df()
        trades_df.to_csv(out_trades, index=False)
        print(f"[输出] 成交记录 → {out_trades} ({len(trades_df)} 条)")

    if out_equity:
        result.equity_curve.to_csv(out_equity, index=False)
        print(f"[输出] 权益曲线 → {out_equity} ({len(result.equity_curve)} 行)")

    if out_logs:
        logs_df = result.to_bar_logs_df()
        logs_df.to_csv(out_logs, index=False)
        print(f"[输出] K线日志 → {out_logs} ({len(logs_df)} 行)")


if __name__ == "__main__":
    main()
