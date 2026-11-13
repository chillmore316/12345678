# 本地量化工作台：开始使用

当前可用的是**本地回测＋历史模拟演练研究版**。它使用本机已存在的训练期历史行情，不接券商、不发送交易指令。实时模拟盘的数据认证与部署尚未完成。

## 1. 打开工作台

首次安装时，在项目根目录运行 `./macos/启动中文回测中心.command --setup`。需要 Apple Silicon、macOS 14 或更新版本、Python 3.11 和可访问的 Python 软件包源。依赖版本已在 macOS 15 上实测。启动器双击时只使用项目环境；安装模式单独运行，结束后再打开工作台。VeighNa 原生基准和 Python 策略隔离环境分别准备。

双击项目中的 `macos/启动中文回测中心.command`，保留启动窗口。浏览器打开 `http://127.0.0.1:8060/research`。已有同一研究服务时会复用；端口被其他程序占用时显示原因，不会关闭未知程序。

展开页面顶部“环境与本地数据”，查看 Python 依赖、分钟目录和历史成功配置。文件名清单只说明存在文件，不能证明全部标的或每分钟覆盖。

## 2. 创建和查看回测

选择“买入并持有”或“双均线交叉”，填写证券、日期、资金比例和成本。默认示例 `000001.SZSE / 2024-01-02 至 2024-01-05` 已实际验收，仅用于演示操作。单次只开放 2024–2025 年、最多31个自然日、单只主板股票的分钟回放。

点击“创建回测任务”，完成后查看权益、回撤、真实分钟K线、实际成交定位、费用、日/月/年度窗口收益与数据核查。失败任务保留日志；不存在的数据不会补成虚假行情。无定义的指标显示“未提供”。

“现金与持仓市值”展示实际账户构成；市值未单独提供时，仅按同一时点的账户权益减现金计算。缺值处断线，零值如实保留。“日净收益分布”只统计报告已有的日收益，固定区间表列出样本数和累计数；少量样本不代表稳定的收益分布。

成交表可以按日期和买卖方向筛选，筛选只改变表格显示，报告总指标与下载CSV仍覆盖整次回测。有完整时间且精确匹配行情的成交，可以点击定位原始分钟；旧格式缺少成交时间时使用记录时间，并标注来源。明确填写但格式错误的成交时间不会被替换，无法匹配时提示原因。切换任务会重置筛选，同一任务的数据刷新会保留筛选。

双均线任务可打开K线上的均线显示，参数来自该任务保存的配置，按已返回的过去行情计算；修改左侧表单不会改变旧报告的均线。均线窗口不足时留空，不会额外获取预热数据。该开关只改变图表显示。

“保守分钟回放”支持两种策略；“VeighNa原生基准”当前仅支持买入持有。两者撮合口径不同，不能根据收益差异评选策略。模拟演练当前使用保守后端。

历史列表支持搜索、状态筛选和排序。“复制参数再实验”会复制完整页面参数并关联父实验，旧报告不改写。修改策略时会解除父实验关联，避免混在同一系列。

### 批量参数对照

在回测中心点「批量与滚动回测」，选择「同区间参数对照」，填写共同配置和每组差异。点「预检方案」确认窗口，再点「提交运行」。可关闭页面并从「已保存的实验」读回进度。提交结果不确定时，用「重试同一提交」保留原编号。已提交的单次回测可从报告入口单独查看或取消。

在项目目录执行以下命令，先检查预设的两组滑点，再明确运行。也可以复制 JSON 模板后修改参数。

```sh
.venv311/bin/python -m research_center.cli batch plan --file research_center/templates/parameter_batch.json
.venv311/bin/python -m research_center.cli batch run --file research_center/templates/parameter_batch.json
```

每批 2–8 组，共用策略、证券、日期和初始资金。任务顺序执行，失败或等待超时会停止提交后续组；已提交的任务仍可从历史列表查看或取消。CLI 先在标准错误输出本次实验编号，标准输出给出最终收据。用编号搜索历史列表，或打开 `http://127.0.0.1:8060/research?experiment=实验编号`。每组仍有独立报告、订单、成交与数据来源。

模板仅比较 2024 年四个交易日的成本敏感性，批量功能目前限 2024–2025 年冻结训练期。不会自动选出最佳参数或读取留出期。

### 固定参数滚动验证

在同一网页选择「固定参数滚动验证」，逐折填写训练和验证日期。选择已登记的 Python 策略时，需要填登记编号并先准备本机隔离环境。

```sh
.venv311/bin/python -m research_center.cli rolling plan --file research_center/templates/rolling_validation.json
.venv311/bin/python -m research_center.cli rolling run --file research_center/templates/rolling_validation.json
```

一次填写 2–4 折，每折由训练段与之后的验证段组成。验证段按时间排列且互不重叠，后续折可以把早期验证日期纳入训练段。所有日期和参数在运行前一并校验，每段最多 31 个自然日，范围限于 2024–2025。每段从相同初始现金和空仓独立回放，不跨段继承仓位或历史。

运行后在网页用实验编号打开同组历史，按任务名称查看各折训练段和验证段的独立报告。不同日期的权益曲线分别查看。模板使用已见历史窗口，适合验收时间分段和报告流程。编排不会根据训练段结果改参数，验证段结果也不构成独立样本外结论。

### 多标的独立对照

在「批量与滚动回测」选择「多标的独立对照」。填写相同策略、资金、成本和日期，再逐行输入 2–4 个支持的主板证券代码。预检通过后提交。每个标的从相同资金和空仓独立回放，逐份查看报告和数据口径。参数与标的由用户预先指定，程序不按收益自动筛选，也不合并组合收益。

```sh
.venv311/bin/python -m research_center.cli symbols plan --file research_center/templates/symbol_comparison.json
.venv311/bin/python -m research_center.cli symbols run --file research_center/templates/symbol_comparison.json
```

### 上传 AI 编写的 Python 策略

打开顶部“Python 策略工作台”，下载模板，让 AI 保留 `build_strategy(symbol, params)`、`on_bar`、`export_state`、`restore_state` 四个接口后修改，保存为 `.py` 文件。登记文件、名称和默认参数，再从版本库选择版本，核对日期、资金和成本，点击“提交历史回放”。登记只做语法检查，不会自动运行；运行时会在独立的 Linux 环境中检查接口和执行代码。

源码上限64 KiB，导出状态为不超过16 KiB的JSON对象。当前提供 Python、pandas、numpy及标准库，不自动安装用户依赖。每根已闭合行情最多返回一笔同标的、100股整手的市价订单。策略自己的参数与资金比例、成交额参与限制分别保存；“策略可见历史上限”表示当前可查看的分钟线数量，不会补读区间之前的行情。

完成后报告会记录源码、接口、执行器和参数版本。失败任务保留日志及本机诊断文件，但不会提供未完成报告或CSV。修改源码后登记新版本，旧源码和旧报告保持不变。Python任务的“复制参数再实验”会返回策略工作台，保留父实验关联。

Python 工作台和历史演练页都有“准备隔离环境”。冷启动后先点击并等待就绪，再手动提交回测或继续演练。也可在项目目录执行 `.venv311/bin/python -m research_center.cli runtime prepare`。准备操作只检查本机专用虚拟机，不提交回测或推进账户。若存在无法核实归属的中断标记，环境会保留隔离状态并显示原因。

## 3. 使用模拟演练账户

在一条已完成的保守回测中点击“创建模拟演练”，或打开 `http://127.0.0.1:8060/replay`。选择当前内置策略版本的已完成保守回测，给账户命名并创建。

新账户从该配置的初始现金、空仓重新运行策略。它冻结源行情和配置，再由共享内核产生订单与成交，不是复制源回测的成交记录。新账户默认暂停，尚未处理任何行情。

- “推进1分钟”：处理下一根实际行情；首次使用会先恢复账户。
- “推进60根行情”：处理下一批已存在行情；缺失分钟不会补造，跨午间/日期的委托依照撮合规则处理。
- “自动推进”：由当前页面连续按批推进；可以随时暂停。
- “暂停并保存”：等待当前请求完成后保存账户，待成交委托保留。
- 重新打开账户：读取持久进度；关闭页面不会启动后台交易。页面关闭后停止自动推进，账户可能仍显示“可推进”，再次打开后可继续或暂停。
- 区间结束：显示“演练完成”，剩余未成交委托不再执行，不能继续推进到数据之外。

重启研究服务后可以继续同一账户。重复推进请求不会重复记账；过期版本请求会被拒绝并刷新。策略或执行源码改变后，未完成旧账户保留只读，需以当前版本新建演练；不会静默替换旧账户代码或重置资金。

页面提供权益、委托、成交、持仓与恢复记录 CSV。研究回测与演练账户在 `results/research_center/` 下分别存放。

### 备份与验证恢复

先关闭自动推进，等当前请求、单次回测和批量任务结束，再关闭研究工作台。确认所有指向该目录的 CLI 任务也已结束。从项目根目录运行：

```sh
backup_dir=$(mktemp -d "$HOME/quant-research-backup.XXXXXXXX")
/usr/bin/ditto results/research_center "$backup_dir/research_center"
printf '备份路径 %s\n' "$backup_dir/research_center"
```

保留完整目录，其中包括报告、索引、账户库和策略档案。验证恢复时将备份复制到新目录，使用独立端口打开。把第一行的备份路径换成上一步打印的路径：

```sh
backup_path='/绝对路径/quant-research-backup.XXXXXXXX/research_center'
restore_dir=$(mktemp -d "$HOME/quant-research-restore.XXXXXXXX")
/usr/bin/ditto "$backup_path" "$restore_dir/research_center"
.venv311/bin/python -m research_center.cli --store "$restore_dir/research_center" serve --port 8061 --verification-only
```

打开 `http://127.0.0.1:8061/research`，在历史列表中找原报告，并打开对应演练页核对进度、权益和 CSV。验证端口拒绝网页提交与账户修改。完成后按 Ctrl+C 关闭验证服务。原 8060 目录与在线模拟盘独立。续跑需通过当前策略版本与运行环境校验；备份读回成功只能说明保存内容可访问。

Python策略使用单独的 `/python-replay` 历史演练页，与内置策略的 `/replay` 分开。它同样使用已完成回测保存的行情，从初始现金和空仓重新产生委托、成交。策略导出的状态会随检查点保存，并在每次隔离会话中恢复；任意策略未导出的全局变量、随机数或外部状态不会自动获得可恢复性保证。

每次推进最多240根，只有隔离策略正常关闭后才提交检查点。响应中断时保留原请求编号和内容，使用“重试同一推进请求”核对，不要直接新建下一次推进。“演练到区间结束”只由当前页面顺序推进已保存的历史行情，关闭页面后不会继续后台交易。

## 4. 正确理解数据警示

当前 `000001` 演示窗口的分钟价格与“标称未复权”的日线缓存四日均不一致；日线缓存自身也未完成价格口径认证。工作台保留原数据并提示冲突，不根据每日收盘比例改造分钟价格。

新增日线对照复用本机两份固定历史缓存清单。原三只标的继续使用原缓存，其余标的只有命中扩展清单时才读取对应日线文件。程序核验清单和目标文件的哈希，并只对照回测日期范围内的收盘。清单覆盖不代表分钟行情完整，收盘一致也不代表复权、成交量单位、公司行动或实时执行已认证。

价格复权、量额单位、完整公司行动、交易日历和可交易状态仍需补证。因此历史模拟演练保持 `RESEARCH_ONLY`，`paper_admission=false`。它能验证操作、状态恢复和账本一致性，不能据此认定策略盈利或实时模拟盘已获准入。

## 5. 命令行检查

页面顶部的“数据候选核查”用于查看单独登记的日线样本。Codex 可用下列命令登记已完成补证的候选，再在页面选择查看：

```bash
.venv311/bin/python -m research_center.cli candidate register --manifest /绝对路径/manifest.json --rows /绝对路径/rows.json --label '日线补证候选'
.venv311/bin/python -m research_center.cli candidate list
.venv311/bin/python -m research_center.cli candidate show CANDIDATE_ID
```

登记会冻结这两份文件的字节快照，保留原文件；重复登记同样内容不会覆盖旧名称和时间。详情重新校验 SHA、字段和数值，预览最多50行，并可下载完整冻结样本。列表最多100条且扫描有上限，更多候选可按编号直接打开。损坏或校验失败时不沿用先前详情。

候选保存在 `results/research_center/data_candidates/`，不进入回测或任何模拟账户。结构通过不等于价格、单位、完整复权或来源许可认证；清单中引用的外部证据不会自动复制，仍应保留原审计目录。此页只读，登记须由本机CLI显式选择文件，不能从网页传入任意本地路径。

在项目根目录执行：

```bash
.venv311/bin/python -m research_center.cli doctor
.venv311/bin/python -m research_center.cli data
.venv311/bin/python -m research_center.launcher --check
.venv311/bin/python -m research_center.cli replay list
.venv311/bin/python -m research_center.cli replay show ACCOUNT_ID
```

每次写操作需要当前 `revision`；每次推进要提供唯一 `request_id`，重试同一请求时保留编号及原参数。

```bash
.venv311/bin/python -m research_center.cli replay create RUN_ID --label '我的历史演练'
.venv311/bin/python -m research_center.cli replay resume ACCOUNT_ID --revision 0
.venv311/bin/python -m research_center.cli replay step ACCOUNT_ID --revision 1 --steps 60 --request-id my-first-step
.venv311/bin/python -m research_center.cli replay pause ACCOUNT_ID --revision 2
```

Python源码和历史账户的对应入口：

```bash
.venv311/bin/python -m research_center.cli python-source register --file strategy.py --name '我的策略' --params '{"fast":5,"slow":20}'
.venv311/bin/python -m research_center.cli run --strategy custom_python --source-id SOURCE_ID --custom-params '{"fast":5,"slow":20}' --wait
.venv311/bin/python -m research_center.cli python-replay create RUN_ID --label 'Python 历史演练'
.venv311/bin/python -m research_center.cli python-replay resume ACCOUNT_ID --revision 0
.venv311/bin/python -m research_center.cli python-replay step ACCOUNT_ID --revision 1 --steps 240 --request-id my-python-step
```

更详细的执行和数据边界见 `docs/LOCAL_BACKTEST_CENTER.md`。上传回测验收见 `research/local_usable_phase13_20261103/`，Python历史账户验收见 `research/local_usable_phase14_20261103/`。
# 查看接源检查历史

研究工作台顶部的“接源检查记录”打开 `/source-check`。这里只展示本机已保存的有界接口检查，刷新不会重新请求供应商，也不会启动策略或模拟账户。可查看接口响应、样本数量、最新样本日期/时间和检查时刻的时间差；这些不是当前行情延迟或完整权限证明。

让 Codex 显式发起新检查：

```sh
.venv311/bin/python -m research_center.cli source check --provider promax --symbol 510300.SH
```

也可显式选择 `--provider datahub`；每次最多两次请求，不重试、不重定向或自动切换源，凭证从本机已有配置读取。无需新请求即可回看：

```sh
.venv311/bin/python -m research_center.cli source list
.venv311/bin/python -m research_center.cli source show 检查编号
```

闭市旧样本、近期但交易日未认证的样本和缺失样本均不会解除模拟盘门禁。来源许可、覆盖、价格/复权口径和分钟标签须另行验证；历史检查成功不等于已部署到实时模拟盘。

