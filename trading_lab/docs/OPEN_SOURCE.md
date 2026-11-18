# 开源组件调研

核查日期：2026-11-17；仓库内容会变化，构建应以 lock 文件锁定版本。

| 项目 | 已核查许可/结构 | 本轮采用方式 |
| --- | --- | --- |
| [LuxAlgo/trade-journal](https://github.com/LuxAlgo/trade-journal) | MIT；TypeScript、SQLite、本地日志、完整交易周期；README说明仍为早期版本 | 借鉴完整开平仓归并、本地存储、日历和日志结构，不直接整体移植或接入券商同步/AI |
| [Eleven-Trading/TradeNote](https://github.com/Eleven-Trading/TradeNote) | GPL-3.0；Node、MongoDB、自托管交易日志 | 参考复盘、筛选和统计交互；本轮未复制代码，不新增MongoDB |
| [klinecharts/KLineChart](https://github.com/klinecharts/KLineChart) | Apache-2.0；可交互K线和指标；主分支10.1.1 | 实际使用npm固定版本9.8.12，按该版本声明文件适配，避免混用10版API |
| [tradingview/lightweight-charts](https://github.com/tradingview/lightweight-charts) | Apache-2.0，含NOTICE及署名要求 | 作为备选；本轮不同时安装两套K线引擎 |
| [akfamily/akshare](https://github.com/akfamily/akshare) | MIT；Python数据接口，近期接口名有变更 | 固定Python依赖；作为显式备用来源，失败不自动替换Tushare缓存 |

数据接口参考：[Tushare日线](https://tushare.pro/document/2?doc_id=27)、[基金日线](https://tushare.pro/document/2?doc_id=127)、[每日涨跌停](https://tushare.pro/document/2?doc_id=183)。第三方中转的覆盖与许可须以实际响应核验，不能由官方接口文档推断。

市场规则参考：[上交所2026交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)、[股票ETF的T+1、交易单位和价位](https://edu.sse.com.cn/service/hotline/qa/c/4956495.shtml)。

除采用的npm/Python软件包外，本轮没有复制上述项目源文件。随软件包保留其许可证；本项目不把数据的获取许可等同于开源软件许可，也不公开再分发行情。
