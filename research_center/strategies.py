"""Small event-driven reference strategies for the local research engine."""

from __future__ import annotations

from strategy.base import Bar, Context, Order, Strategy


def _target_quantity(cash: float, reference_price: float, context: Context) -> int:
    """Size a long-only target as whole 100-share lots without using future prices."""
    params = context.params
    lot_size = 100
    target_fraction = float(params.get("target_fraction", 0.5))
    commission_rate = float(params.get("commission_rate", 0.00025))
    min_commission = float(params.get("min_commission", 5.0))
    slippage_bps = float(params.get("slippage_bps", 5.0))
    price = reference_price * (1.0 + slippage_bps / 10_000.0)
    target_cash = max(0.0, cash) * target_fraction
    quantity = int(target_cash / (price * lot_size)) * lot_size
    while quantity > 0:
        notional = quantity * price
        commission = max(notional * commission_rate, min_commission)
        if notional + commission <= cash + 1e-9:
            return quantity
        quantity -= lot_size
    return 0


class BuyHoldStrategy(Strategy):
    """Enter once at the first eligible signal and hold through the run."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def on_bar(self, bar: Bar, ctx: Context) -> list[Order]:
        if ctx.params.get("_pending_side"):
            return []
        position = ctx.positions.get(self.symbol, {})
        if float(position.get("quantity", 0.0)) > 0:
            return []
        quantity = _target_quantity(
            ctx.cash, float(bar.ohlcv[self.symbol]["close"]), ctx
        )
        if quantity <= 0:
            return []
        return [Order(symbol=self.symbol, side="buy", quantity=quantity)]


class SmaCrossStrategy(Strategy):
    """Long-only moving-average cross using completed bars in the context."""

    def __init__(self, symbol: str, fast: int, slow: int) -> None:
        self.symbol = symbol
        self.fast = fast
        self.slow = slow

    def on_bar(self, bar: Bar, ctx: Context) -> list[Order]:
        if ctx.params.get("_pending_side") or ctx.params.get("_sale_filled_this_bar"):
            return []
        history = ctx.history.get(self.symbol)
        if history is None or len(history) < self.slow + 1:
            return []

        closes = history["close"].astype(float)
        previous = closes.iloc[:-1]
        previous_fast = float(previous.tail(self.fast).mean())
        previous_slow = float(previous.tail(self.slow).mean())
        current_fast = float(closes.tail(self.fast).mean())
        current_slow = float(closes.tail(self.slow).mean())
        position = ctx.positions.get(self.symbol, {})
        quantity_held = int(position.get("quantity", 0))
        available_to_sell = int(position.get("available_to_sell", 0))

        crossed_up = previous_fast <= previous_slow and current_fast > current_slow
        crossed_down = previous_fast >= previous_slow and current_fast < current_slow
        if quantity_held == 0 and crossed_up:
            quantity = _target_quantity(
                ctx.cash, float(bar.ohlcv[self.symbol]["close"]), ctx
            )
            if quantity > 0:
                return [Order(symbol=self.symbol, side="buy", quantity=quantity)]
        # If a bearish state starts on the entry day, wait for a later signal
        # bar after T+1 becomes available; do not carry a stale order overnight.
        if quantity_held > 0 and available_to_sell > 0:
            if crossed_down or current_fast < current_slow:
                return [
                    Order(
                        symbol=self.symbol,
                        side="sell",
                        quantity=available_to_sell,
                    )
                ]
        return []


STRATEGIES: dict[str, type[Strategy]] = {
    "buy_hold": BuyHoldStrategy,
    "sma_cross": SmaCrossStrategy,
}

