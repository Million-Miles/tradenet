"""
策略配置参数 — 动态网格交易策略 V2.1
"""
import configparser
import os
from dataclasses import dataclass, field

# 固定常量，不可配置
RSI_OVERBOUGHT: float = 70.0
RSI_OVERSOLD: float = 30.0

INTERVAL_MINUTES: dict = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360,
    "8h": 480, "12h": 720, "1d": 1440,
}


@dataclass
class StrategyConfig:
    # ── 交易标的 ──────────────────────────────────────────────────
    symbol: str = "BTCUSDT"

    # ── 时间周期 ──────────────────────────────────────────────────
    timeframe: str = "1m"
    timeframe_minutes: int = 1          # 用于周期修正系数计算

    # ── RSI 参数 ──────────────────────────────────────────────────
    rsi_period: int = 14
    # rsi_overbought = 70  (固定，见模块常量 RSI_OVERBOUGHT)
    # rsi_oversold   = 30  (固定，见模块常量 RSI_OVERSOLD)
    rsi_divergence_depth: float = 20.0  # 前次超卖RSI低于此值 → 深度超卖，不判断背离
    second_signal_valid_bars: int = 100  # 两次超卖间隔超过此K线数 → 重置为首次信号

    # ── ATR 参数 ──────────────────────────────────────────────────
    atr_period: int = 14
    atr_spike_multiplier: float = 3.0   # 当前ATR > 均值 × N 倍触发保护
    atr_spike_lookback: int = 20        # ATR均值计算回溯K线数

    # ── 网格挂单参数 ──────────────────────────────────────────────
    first_order_coeff: float = 0.5      # 第一单距现价 = 系数 × ATR
    grid_spacing_coeff: float = 1.0     # 基础间距 = 系数 × ATR

    # ── 风险管理 ──────────────────────────────────────────────────
    max_position_ratio: float = 1.0     # 已用保证金占当前本金上限
    take_profit_usdt: float = 0.0       # 止盈金额（USDT），0 = 不启用
    take_profit_pct: float = 0.0        # 止盈比例（本金百分比），0 = 不启用；任一满足即触发
    single_strategy_stop_loss: float = 0.20   # 单次策略止损：浮亏超过本金此比例
    global_stop_loss: float = 0.30      # 全局止损：累计总亏损超过初始本金此比例
    min_trade_opportunities: int = 50   # 最低下单机会次数（用于仓位计算）

    # ── 合约参数 ──────────────────────────────────────────────────
    leverage: int = 10
    contract_value: float = 1.0         # 每张合约价值（USDT永续合约=1，即数量即张数）

    # ── 回测参数 ──────────────────────────────────────────────────
    initial_capital: float = 10000.0    # 初始本金（USDT）
    commission_rate: float = 0.0005     # 手续费率（吃单 0.05%）
    slippage_rate: float = 0.0002       # 市价单滑点（0.02%）

    # ── 合约精度 ──────────────────────────────────────────────────
    min_qty_step: float = 0.001         # 最小数量步长（BTC）
    price_tick: float = 0.1             # 最小价格精度

    @classmethod
    def from_ini(cls, strategy_ini: str = "config.ini", backtest_ini: str = "backtest.ini") -> "StrategyConfig":
        """
        从两个配置文件加载参数：
          config.ini   — 策略参数（用户调参）
          backtest.ini — 回测/模拟参数（数据、精度、手续费等）
        """
        for path in (strategy_ini, backtest_ini):
            if not os.path.exists(path):
                raise FileNotFoundError(f"找不到配置文件: {path}")

        ini = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        ini.read([strategy_ini, backtest_ini], encoding="utf-8")

        def get(section, key, fallback):
            return ini.get(section, key, fallback=str(fallback))

        timeframe = get("timeframe", "interval", "1m")

        return cls(
            symbol                    = get("data",      "symbol",                    "BTCUSDT"),
            timeframe                 = timeframe,
            timeframe_minutes         = INTERVAL_MINUTES.get(timeframe, 1),
            # ── 策略参数（来自 config.ini）──────────────────────
            rsi_period                = int(get("rsi",      "period",                    14)),
            rsi_divergence_depth      = float(get("rsi",    "divergence_depth",          20.0)),
            second_signal_valid_bars  = int(get("rsi",      "second_signal_valid_bars",  100)),
            atr_period                = int(get("atr",      "period",                    14)),
            atr_spike_multiplier      = float(get("atr",    "spike_multiplier",          3.0)),
            first_order_coeff         = float(get("grid",   "first_order_coeff",         0.5)),
            grid_spacing_coeff        = float(get("grid",   "spacing_coeff",             1.0)),
            max_position_ratio        = float(get("risk",   "max_position_ratio",        1.0)),
            take_profit_usdt          = float(get("risk",   "take_profit_usdt",          0.0)),
            take_profit_pct           = float(get("risk",   "take_profit_pct",            0.0)),
            single_strategy_stop_loss = float(get("risk",   "single_strategy_stop_loss", 0.20)),
            global_stop_loss          = float(get("risk",   "global_stop_loss",          0.30)),
            min_trade_opportunities   = int(get("risk",     "min_trade_opportunities",   50)),
            leverage                  = int(get("contract", "leverage",                  10)),
            # ── 回测参数（来自 backtest.ini）────────────────────
            initial_capital           = float(get("account",       "initial_capital",    10000.0)),
            commission_rate           = float(get("simulation",     "commission_rate",    0.0005)),
            slippage_rate             = float(get("simulation",     "slippage_rate",      0.0002)),
            contract_value            = float(get("contract_spec",  "contract_value",     1.0)),
            min_qty_step              = float(get("contract_spec",  "min_qty_step",       0.001)),
            price_tick                = float(get("contract_spec",  "price_tick",         0.1)),
            atr_spike_lookback        = int(get("contract_spec",    "atr_spike_lookback", 20)),
        )

    def validate(self) -> None:
        """基本参数校验"""
        assert self.leverage >= 1, \
            "杠杆必须 >= 1"
        assert 0 < self.single_strategy_stop_loss < 1, \
            f"单次策略止损必须在 0~1 之间，当前值: {self.single_strategy_stop_loss}"
        assert 0 < self.global_stop_loss < 1, \
            f"全局止损必须在 0~1 之间，当前值: {self.global_stop_loss}"
        assert self.global_stop_loss > self.single_strategy_stop_loss, \
            (f"config.ini 参数错误：global_stop_loss ({self.global_stop_loss}) "
             f"必须大于 single_strategy_stop_loss ({self.single_strategy_stop_loss})。\n"
             f"  原因：全局止损是最后防线，阈值必须比单次止损更大。\n"
             f"  建议：single_strategy_stop_loss=0.20, global_stop_loss=0.50")
        assert self.first_order_coeff > 0, \
            "first_order_coeff 必须 > 0"
        assert self.grid_spacing_coeff > 0, \
            "spacing_coeff 必须 > 0"
        assert self.timeframe_minutes >= 1, \
            "timeframe_minutes 必须 >= 1"
