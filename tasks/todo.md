# 项目梳理 — 待办事项

生成时间：2026-03-20

---

## P0 — 必须修复（影响正确性）

- [x] **strategy.py** `unrealized_pnl` 属性返回硬编码0，与接口语义不符，已删除
- [x] **strategy.py** `_calc_qty()` 缺少 ATR=0 / capital≤0 保护，已加入守卫返回 0.0
- [x] **backtest.py** 夏普比率公式复核：`(mean/std)×√年化周期数` 是标准公式，对24/7加密货币市场正确，无需修改
- [x] **config.ini** `spacing_coeff` 和 `first_order_coeff` 均从 1 改为 0.2，适配1m图

## P1 — 应该修复（影响功能/维护性）

- [x] **models.py** 删除未使用的 `StopThreshold` 枚举
- [x] **models.py** 删除未使用的 `LONG_FIRST / SHORT_FIRST` SignalType，同步清理 strategy.py 引用
- [x] **models.py / strategy.py** `BarLog.signal` 现已在 `_make_log()` 中记录实际 SignalType
- [x] **strategy.py** RSI信号检测重复代码提取为 `_check_episode_signal()` 通用方法
- [x] **config.py / run_backtest.py** `INTERVAL_MINUTES` 统一到 config.py，run_backtest.py 直接导入
- [x] **backtest.py** `print_event_log()` 改用 `log.signal` 字段，不再解析 action 字符串

## P2 — 增强功能

- [x] 添加基础单元测试：25个测试全部通过（tests/test_indicators.py + tests/test_strategy.py）
- [x] CLI 支持命令行覆盖单个参数（--capital, --leverage, --first-coeff, --spacing-coeff, --take-profit, --stop-loss）
- [x] `take_profit_pct` 按本金比例止盈，与 `take_profit_usdt` 任一满足即触发
- [ ] 参数网格搜索：自动遍历 spacing_coeff / first_order_coeff 组合
- [x] backtest.py `profit_factor` 为 inf 时显示「∞（无亏损交易）」

---

## 已完成

- [x] 删除 FIRST 信号，只保留背离A + 深度B 二次信号
- [x] 修复 ORDER_STOP 状态机卡死问题（无持仓时回 IDLE）
- [x] 修复 total_pnl 显示（改为 final_equity - initial_capital）
- [x] 修复事件日志时间戳（row["timestamp"] 而非 row["index"]）
- [x] 止盈改为固定 USDT 金额（take_profit_usdt）
- [x] 背离信号增加价格回头确认（close > curr_price_low）
