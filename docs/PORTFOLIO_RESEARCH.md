# 共享资金组合历史研究（开发中）

组合内核支持2至4只当前允许的主板股票，1分钟、2024–2025年、每次最多31个日历日。它使用一份现金及逐标的买入批次，先按配置中的标的顺序撮合上一分钟订单，再用同一现金快照生成信号。时间轴缺口不会补成可交易价格。

当前中文页面是只读历史报告，入口为工作台“组合历史报告”，或 `/portfolio`。可查看共享资金权益和现金、逐标的持仓、成交、费用与数据警示，通过分享链接重开记录。它不会提交任务或操作演练账户。文件哈希只能发现意外变化，不能认证账本或数据。

## 手动运行历史研究

真实行情须由用户合法获取并满足既有数据要求。创建本机JSON配置，例如：

```json
{
  "symbols": ["000001.SZSE", "000002.SZSE"],
  "start": "2024-01-02",
  "end": "2024-01-05",
  "strategy": "buy_hold",
  "initial_cash": 100000,
  "commission_rate": 0.00025,
  "min_commission": 5,
  "slippage_bps": 5,
  "params": {"target_fraction": 0.5, "max_turnover_participation": 0.01}
}
```

```sh
.venv311/bin/python -m research_center.portfolio_cli plan --file portfolio-config.json
.venv311/bin/python -m research_center.portfolio_cli run --file portfolio-config.json
```

`plan` 仅校验配置，不读取行情。`run` 的回执、配置、数据清单、状态及结果默认写入 `results/research_center_portfolios`，与单股账本分开。完成后刷新中文页面可查看。`--store` 可指定独立研究目录；自定义目录不会自动出现在默认网页历史中。

`params.target_fraction` 是生成买单时的共享现金预算上限，默认按标的等分。可用 `allocation_weights` 明确各标的份额，合计不能超过该上限。这是基于现金的信号预算，不是持续再平衡的目标持仓权重；同一分钟现金不足时按 `symbols` 顺序优先成交。

## 尚未完成的部分

网页提交、持久化异步执行、取消/限时、源码快照及完整组合演练账户仍待实现。本机参考引擎的操作系统级RSS限制也未验收。当前版本不应被视为这些能力已经完成。

已观察到的数据或价格不一致会继续标为 `mismatch`，其余未核实输入保持 `unverified`。T+1、整手、费用和成交额容量代理的实现，不证明停复牌、涨跌停、复权、公司行动及成交语义已经认证。始终保持 `RESEARCH_ONLY`、`paper_admission=false`；不连接券商、不操作8051在线账户。
