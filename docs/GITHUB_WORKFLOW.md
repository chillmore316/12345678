# GitHub 参考项目与版本管理

## 可借鉴的实现

| 参考项目 | 借鉴点 | 本项目的落点 |
|---|---|---|
| [VeighNa Portfolio Strategy](https://github.com/vnpy/vnpy_portfoliostrategy) | 组合事件驱动、回测接口和订单生命周期 | 保留现有 VeighNa 基准，参考其事件接口编排组合研究。撮合仍由本项目验证 A 股 T+1、整手、费用和成交容量。 |
| [Microsoft Qlib 工作流](https://github.com/microsoft/qlib/blob/main/docs/component/workflow.rst) | 配置驱动、日期分段、每次执行归档 | 在现有研究任务之上记录批量参数、训练及验证区间、资源上限和每轮报告。现有冻结留出期保持关闭。 |
| [Backtesting.py](https://github.com/kernc/backtesting.py) | 简洁的策略接口、参数优化和结果展示 | 参考策略模板、参数敏感性和报告交互。保留现有中文工作台、隔离执行和撮合口径。 |
| [RQAlpha](https://github.com/ricequant/rqalpha) | 多证券回测和可替换组件 | 参考数据适配与撮合模块之间的接口，后续用于测试交易规则、数据缺口和拒单原因。 |

首选借鉴 Qlib 的实验组织方式。上述项目作为设计参考，当前未引入新依赖或第三方源码。

## 代码仓库和首次提交

当前工作区已有 Git 历史但没有远端。旧提交包含行情缓存、模拟账户文件、日志和策略压缩归档。Git 忽略规则只影响未跟踪文件，无法从已经提交的历史中移除内容。因此首次远端基线应从经过审阅的代码清单创建，旧历史留在本机。

- 代码仓库中维护源码、合成测试、无密钥配置模板和操作文档。实际行情、策略运行凭证、SQLite、账户账本、报告、调试记录和本机用户路径留在本机。
- 创建首个基线前逐项审阅文件清单，检查硬编码的路径、账户标识和配置值。用合成测试及文档核验基线从干净检出可用。
- 后续使用 `codex/<任务>` 分支，通过 PR 审查改动与验证结果后合入 `main`。用标签标识已验收版本。发布记录分别列明合成测试、真实历史回放、在线运行和数据准入。
- 远端只用于代码版本管理，现有 8051 在线影子服务和本机研究数据继续独立运行。

## 2026-11-12 插件复核与代码级参考

GitHub 插件本次可读取公开参考代码；当前账户可写目标仅见 `chillmore316/12345678`，为公开空仓库。用户随后明确授权使用该公开仓库。公开发布仅同步经过审阅的纯代码基线，不上传本地混合历史及研究数据。

本地独立代码基线位于 `.git/quant-research-baseline`，现有已验收提交为 `ad559ff`，离线 Git bundle 为 `.git/quant-research-baseline.bundle`。原工作区历史及未提交改动留在原处；新增组合代码仍在开发与验收中，未纳入该基线。

本次通过插件实际读取了以下源码：

- [VeighNa backtesting.py，固定提交326ad694](https://github.com/vnpy/vnpy_portfoliostrategy/blob/326ad694ac8439807690f085f8d1c36337a705e3/vnpy_portfoliostrategy/backtesting.py)：`new_bars` 按同一时刻组织多标的行情，先调用 `cross_limit_order` 再调用策略 `on_bars`。这一事件顺序可作参考。但其缺行情分支会用前一根收盘价构造当前bar；本项目不沿用该填充行为，组合数据必须有相同的真实观测时间轴。
- [Qlib recorder.py](https://github.com/microsoft/qlib/blob/main/qlib/workflow/recorder.py)：`Recorder` 定义 `start_run`、`end_run`、`save_objects` 等实验生命周期接口，可参考其运行编号、配置和产物归档方式。该链接指向活动分支；本次未复制源码或新增依赖。

后续远端流程：按用户授权使用公开仓库，核对权限及待发布清单，再同步纯代码基线，开发使用 `codex/` 分支，独立复核后建立 PR。远端提交不包含真实行情、运行账本、凭证、机器路径配置或原始混合历史。

## 2026-11-13 公开基线发布

用户授权的公开仓库为 [chillmore316/12345678](https://github.com/chillmore316/12345678)。已通过插件发布提交 `bcee542f4744fff38f8d573cb58e255341eb0989`，当前101文件树为 `6de97c59f41a79a5f1d8ef538f56265afad93e1f`。远端main引用、逐文件blob哈希和权限均与审阅后的本地树一致。恢复时重新运行315项Python和8项Node测试通过。

本机shell的GitHub DNS解析失败，普通git fetch未成功；通过插件读取远端Git提交及树元数据，按原始Git对象校验SHA后在独立本地代码仓库建立对应对象和origin/main。该仓库main已跟踪远端同一提交，旧本地纯代码历史保留在codex/local-prepublication。原混合工作区未设置远端，也未上传旧历史。组合功能仍在开发验收，尚未进入公开main。
