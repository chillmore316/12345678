# 本地量化工作台

中文图形界面提供历史策略回测、结果对照和可恢复的历史模拟演练。此仓库基线维护研究工作台源码和合成测试。行情、账户、验收产物和在线影子服务留在各自的本地环境。

首次安装需要 Apple Silicon、macOS 14 或更新版本和 Python 3.11。在仓库根目录执行：

```sh
./macos/启动中文回测中心.command --setup
```

双击 `macos/启动中文回测中心.command`，或运行 `.venv311/bin/python -m research_center.launcher`，访问 [中文回测中心](http://127.0.0.1:8060/research)。若本机另有研究服务，请使用独立端口，例如 `.venv311/bin/python -m research_center.cli serve --port 8061`。

真实回测需要合法授权的本机分钟行情。请参照[操作指南](docs/QUANT_QUICKSTART.md)和[数据要求](docs/LOCAL_BACKTEST_CENTER.md)。第三方 Python 策略运行需要单独准备隔离环境，VeighNa 原生基准还需要 VeighNa 依赖。仓库不含付费行情、凭证、现有账户或真实验收报告。

完整 Python 测试还需要 VeighNa 4.4.0 和 vnpy_portfoliostrategy 1.3.0。

```sh
.venv311/bin/python -m unittest discover -s tests
node --test tests/*.cjs
```

研究工作台保持 `RESEARCH_ONLY` 和 `paper_admission=false`。历史演练使用本机回放，不连接券商。
