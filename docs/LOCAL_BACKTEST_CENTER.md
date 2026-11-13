# 中文本地回测中心

这是 P0–P2 的受限研究工作流，不是模拟盘准入工具。默认监听本机，不接券商，不读取/修改现有模拟账户。单标的主板股票、1分钟、2024–2025训练期，每次最多31个日历日；完整A股规则、ETF、多标的、任意上传、策略部署尚未接通。

## 打开网页

双击 `macos/启动中文回测中心.command`。该窗口持续运行本机8060研究服务，关闭窗口可停止网页；已提交的有界研究worker继续留存结果。它不启动8051行情盘。也可在项目根目录执行：

```bash
.venv311/bin/python -m research_center.cli serve --port 8060
```

打开 http://127.0.0.1:8060/research 。`Ctrl+C` 关闭；再次执行同命令重启，历史保留。8051仓库源码的 `/research` 只跳转到8060，不承载研究任务或结果库。本次未部署运行副本，当前8051尚无该入口。

## Codex与命令行调用

```bash
.venv311/bin/python -m research_center.cli run --strategy buy_hold --symbol 000001.SZSE --start 2024-01-02 --end 2024-01-05 --wait
.venv311/bin/python -m research_center.cli run --strategy sma_cross --symbol 000001.SZSE --start 2024-01-02 --end 2024-01-05 --params '{"fast":5,"slow":20,"target_fraction":0.3}' --wait
.venv311/bin/python -m research_center.cli list
.venv311/bin/python -m research_center.cli show RUN_ID
.venv311/bin/python -m research_center.cli cancel RUN_ID
```

每次返回新的run_id和可打开的report_url；CLI默认结果链接指向8060，若自行改端口，按所用端口打开同一个run_id。网页与CLI使用同一提交/执行/保存代码。API POST `/api/research/runs` 收JSON配置；GET同路径列记录，GET `/api/research/runs/RUN_ID` 读明细，POST `/api/research/runs/RUN_ID/cancel` 请求取消。

## 数据与保存

原始聚宽月批次只读；不下载、不改原始数据。默认结果放 `results/research_center/`：SQLite永久索引、每run独立目录、来源与依赖、代码快照/hash、JSON曲线与明细、CSV、执行日志。不要删除该目录。旧 `results/runs/` 与账户账本不受影响，旧格式结果尚未自动导入。

worker是受限内置策略进程，不是任意Python沙箱。串行任务、最多31天、CPU180秒/墙钟240秒；取消在读取/回放检查点生效。异常与取消保留记录。进程被系统强杀可能保留running，下次提交检查worker是否退出并记interrupted；不能把这种记录当完成。macOS未使用可能破坏Arrow mmap的虚拟内存限制，单代码/时间窗/行数和串行限制控制工作量，尚无操作系统级RSS硬限制。

## 结果的正确解释

真实数据回放证明的是软件链路，不证明策略收益有效。数据的qfq vintage和volume单位尚未证实，缺完整证券状态/涨跌停/停牌元数据与公司行动；因此一律RESEARCH_ONLY、paper_admission=false。保守撮合假设和具体指标口径见每次结果的limitations/data_manifest；空缺指标显示—，不估算补齐。2026保持留出；没有用本轮结果选择生产策略。

后续优先接统一VeighNa结果适配与冻结的原价/公司行动/可交易性数据，再验证PaperMatcher一致性，最后才开放经审核版本部署。手动导入Python需真实沙箱，不能仅靠AST校验或普通子进程放行。

## 第二阶段：统一后端与实验版本

页面可选择“保守分钟回放”或“VeighNa原生基准”；当前 VeighNa 只接通内置买入持有基准，尚未接入 v9.17/v9.19/v9.20 ETF 轮动策略。原生后端使用 VeighNa 4.4.0 / PortfolioStrategy 1.3.0 的真实订单和成交，通过内存注入本地分钟数据，不读取或改写已有 VeighNa 数据库。首次导入在 worker 独立临时空配置中完成，不复用项目或个人设置。

两个后端复用同一策略核心，并共用提交、源码快照、结果存储与报告接口，但撮合模型不同：reference 使用下一连续分钟的不利极值加滑点；vnpy 使用受限原生限价撮合，并在原生成交额之外单独扣佣金和滑点现金成本。原生整笔容量检查使用成交额代理，不做部分成交；原生成交价和 ID 原样保留。不得把后端间的收益差异当成策略优劣。数据复权、容量、涨跌停及证券状态仍未认证。

```bash
.venv311/bin/python -m research_center.cli strategies
.venv311/bin/python -m research_center.cli run --backend vnpy --strategy buy_hold --symbol 000001.SZSE --start 2024-01-02 --end 2024-01-05 --params '{"target_fraction":0.3}' --label '原生基准' --wait
.venv311/bin/python -m research_center.cli run --backend reference --strategy buy_hold --symbol 000001.SZSE --start 2024-01-02 --end 2024-01-05 --params '{"target_fraction":0.2}' --parent-run-id PARENT_RUN_ID --label '参数对照' --wait
```

每次新运行自动保存由策略核心源码推导的 `strategy_version`、后端及 `experiment_id`。`--parent-run-id` 关联同一策略的旧实验并继承系列；旧实验记录不会被修改。也可以为新系列指定 `--experiment-id`（1–64位字母、数字、下划线或短横线）。任意代码或未登记后端仍不允许执行。旧的无版本记录保持“未提供”，不会补造版本。

详情页新增真实分钟 K 线：选择交易日和30/60/90/120根窗口，点击成交表的“定位K线”跳转到相应窗口。标记只在同一证券的成交时间与唯一行情结束时间精确对应时绘制；缺失、重复或不同代码时不挪到邻近K线。该图展示回放数据，不代表实时行情。

## 第三阶段：统计报告与数据证据

每条成功任务保存 `analytics` 和 `data_readiness`。旧成功任务在读取时仅在内存中派生这两项，标记 `derived_read_only`，不改写原 result.json、数据库指标或源码快照；原始账本不一致时显示统计不可用。失败任务不会生成成功报告。

网页显示日收益、所选窗口内的月/年收益、Sortino、Calmar、FIFO 平仓配对胜率/盈利因子/盈亏比、双边区间换手、费用，以及标的价格指数。无定义的值保持空值并列明原因。不足一年不计算年化/Calmar；月/年始终标记非完整期间。价格指数从同份分钟行情首个 close 归一化，不含成本、不模拟订单，不能称为可交易买入持有。reference 滑点已包含于成交价，不再扣款；VeighNa 独立滑点现金扣款进入 FIFO 成本。

CLI 精简报告：`.venv311/bin/python -m research_center.cli report RUN_ID`。新增同源 CSV 导出为 `daily_returns.csv`、`monthly_returns.csv`、`yearly_returns.csv`、`closed_trades.csv`；旧报告导出也只在内存生成。

数据核查仅对照已保存结果和三只股票的固定日线探测缓存，完整性校验不等于数据认证。只用实际 15:00 bar 对照同日日线，不用盘中末值替代，不反推分钟复权系数。2024-01-02 至 01-05 的 000001 分钟与标称未复权日线四日均不一致；日线缓存自身 `price_basis_verified=false`。所有结果保持 `RESEARCH_ONLY`，`paper_admission=false`。后续应先取得能追溯历史版本的原始价格及公司行动、日历和可交易状态，再对齐模拟撮合。

## 第四阶段：可恢复的历史模拟演练

操作入口见 [开始使用](QUANT_QUICKSTART.md)。`/replay` 与 `/research` 共用8060研究服务及同源保护，界面可从完成的reference回测创建独立账户。`ReplayAccounts`固定写到RunStore下的 `replay_accounts/accounts.sqlite3`；不提供网页自定目录，不接任何在线账户。源run/result/config/provenance/行情及执行源码均有冻结摘要。

`replay_kernel`把原保守回放规则提取为JSON状态机，批量回测和增量账户共用同一转换；暂停不取消待成交委托，最后一根才finalize。完整bars、权益、订单、成交及持仓可按块或JSON恢复后继续。哈希检测意外变化，不是任意代码沙箱。原终点订单status/reason保留兼容，账户finalized明确禁止继续。

账户推进使用SQLite `BEGIN IMMEDIATE`、FULL同步、revision和request_id，把账户、游标、事件与紧凑去重确认一起提交。重放旧请求返回当前一致详情和原确认信息，不重新推进，不持久化每个请求的整份大报告。真实进程在账户更新与提交间被终止的合成测试验证回滚，重试恰一次。源码变化后旧账户仍可读/导出，未完成账户禁止继续。

每次只向内核提交游标之后最多240根冻结行情；API只返回已处理bars，不返回未处理行。当前费用由内核累计，保留source数据清单但不抄入源回测最终费用。源价格冲突不被消除，mode始终RESEARCH_ONLY、paper_admissionfalse；这是历史模拟演练，不是实时纸盘部署。

本机Pandas3/Arrow在HTTP请求线程间隐式构造字符串列时曾发生原生崩溃。内核改为数值矩阵、object列标签及显式时间索引，避免该隐式路径；增加跨线程且禁止隐式Arrow字符串分配的回归测试。该修复没有改变成交规则，保存的两条真实回测与新同核/增量结果逐字段一致。

新增 `doctor`、`data` CLI和只读环境/文件名清单API。启动器验证服务标识再复用端口；未知占用不终止。该doctor只检查本地软件，不认证数据来源、覆盖或模拟盘准入。
