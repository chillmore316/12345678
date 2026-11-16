# 组合历史模拟演练

本模块用一份共享现金逐步回放已完成的组合回测，独立保存现金、持仓批次、委托、成交、权益和操作事件。它不连接行情服务或券商，不向原在线账户发信号。模式固定为 `RESEARCH_ONLY`、`paper_admission=false`。

## 中文页面

1. 启动工作台，打开 `/portfolio`，预检并完成一次当前版本的组合回测。
2. 在报告中点击“创建组合历史演练”，或打开 `/portfolio-replay` 选择来源。
3. 创建账户后，点击恢复，再按需推进 1、60 或 240 帧。一帧是同一分钟所有组合标的的共同观测，不是一个标的一根行情。
4. 暂停或关闭页面后，进度留在本机。重新打开账户可继续查看或推进；刷新页面不会自动执行、自动交易或重发写请求。
5. 查看共享现金和权益、逐标的持仓、委托、成交及事件；可导出相应 CSV。

“恢复”只允许后续显式推进，不启动后台自动运行。回放只消费已冻结的历史帧；未来帧不提前展示。末帧完成后，账户不可继续推进，也不强行平仓制造额外成交。

## 请求丢失与代码变化

创建和推进使用持久化请求编号去重。响应丢失时保留原编号和完整参数，使用“显式重试同一请求”查询或完成原操作，不生成另一笔推进。遇到明确版本冲突时，刷新账户，并按页面提示处理已确认未提交的请求。

创建时核对回测归档完整性及执行源码。代码变化后旧账户可读，推进会停止；应使用当前版本重跑回测，再创建独立账户。不会执行历史归档中的 Python 源码。

## Codex 和本机 CLI

使用实际回测编号与账户编号替换示例占位符；每次新操作选择独立请求编号，重试保留原编号及参数：

```sh
.venv311/bin/python -m research_center.cli portfolio-replay list
.venv311/bin/python -m research_center.cli portfolio-replay create RUN_ID --label 组合演练 --request-id create-example-1
.venv311/bin/python -m research_center.cli portfolio-replay show ACCOUNT_ID
.venv311/bin/python -m research_center.cli portfolio-replay resume ACCOUNT_ID --revision 0
.venv311/bin/python -m research_center.cli portfolio-replay step ACCOUNT_ID --revision 1 --steps 60 --request-id step-example-1
```

`revision` 必须取自刚读取的账户，示例数字不能用于已有账户。创建与推进也可通过 `/api/research/portfolio-replays` 本机 API 执行。账户保存在研究存储目录旁的独立 `_portfolio_replays` 目录，不覆写源回测、分钟库或其他演练账户。

## 结果边界

历史恢复核账与程序测试不能证明行情已认证。源回测的 `mismatch` 或 `unverified` 警示继续保留。下一分钟成交、T+1、整手、费用和成交额容量代理仍沿用组合内核；停复牌、涨跌停、复权、公司行动与实时行情的认证须单独完成。本模块不替代实时模拟盘的持续行情、断线停止及端到端验收。
