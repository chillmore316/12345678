"""策略层：基类 + 订单/上下文数据结构。回测与模拟盘共用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class Order:
    symbol: str
    side: str           # "buy" / "sell"
    quantity: float
    order_type: str = "market"            # market / limit
    limit_price: Optional[float] = None


@dataclass
class Bar:
    """单个时刻的全市场快照。ohlcv: symbol -> dict。"""
    datetime: pd.Timestamp
    ohlcv: Dict[str, dict] = field(default_factory=dict)

    def close(self, symbol: str) -> float:
        return self.ohlcv[symbol]["close"]


@dataclass
class Context:
    """传给策略的运行上下文。"""
    datetime: pd.Timestamp
    cash: float
    positions: Dict[str, dict]            # symbol -> {"quantity","avg_cost"}
    history: Dict[str, pd.DataFrame]      # symbol -> 到当前 bar 的历史 K 线
    params: dict = field(default_factory=dict)


class Strategy:
    """策略基类。实现 on_bar 返回订单列表。"""

    def on_bar(self, bar: Bar, ctx: Context) -> List[Order]:
        raise NotImplementedError
