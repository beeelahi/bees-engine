"""
"Sell high, rebuy lower" grid for one coin (Python 3.9 stdlib only). Used by the simulator and the live bot.

Ladder: sell levels at anchor x (1 + step)^k, k = 1..levels. Every sell that fills is paired with a rebuy one
step lower (same quantity). When the rebuy fills, the sell at that level is re-armed. So each completed round
sells `size` coins and buys them back `step` lower: profit = size x (sell - buy) - fees, paid in USDT.
Coins can only leave the inventory through sells whose rebuy has not filled yet; their count is capped so the
inventory never falls below `floor` coins. USDT is only spent on rebuys of coins we already sold.
Re-centering: when no round is open and the price has drifted below the lowest sell level by more than
`recenter_steps` steps, the ladder moves down to the market (so the bot keeps working in a falling market);
it never moves up while coins are out, which keeps the worst case at the floor.
"""


class Grid:
    def __init__(self, anchor, step, levels, size, floor, start_coins, start_usdt, fee=0.0005, recenter_steps=3, through=0.0):
        self.step, self.levels, self.size, self.floor = step, levels, size, floor
        self.coins, self.usdt, self.fee, self.recenter_steps = start_coins, start_usdt, fee, recenter_steps
        self.through = through          # conservative fills: price must trade this far beyond the order
        self.set_anchor(anchor)
        self.open = {}            # level -> rebuy price (a sell at this level filled, waiting to buy back)
        self.rounds = 0
        self.profit = 0.0
        self.min_coins = start_coins
        self.sold_value = 0.0

    def set_anchor(self, a):
        self.anchor = a
        self.sell_px = {k: a * (1 + self.step) ** k for k in range(1, self.levels + 1)}

    def max_open(self):
        return int((self.coins + len(self.open) * self.size - self.floor) // self.size)

    def orders(self):
        """Current resting orders: list of (side, level, price, qty)."""
        out = [("BUY", k, px, self.size) for k, px in self.open.items()]
        room = self.max_open() - len(self.open)
        for k in sorted(self.sell_px):
            if room <= 0:
                break
            if k not in self.open and self.coins - self.size >= self.floor:
                out.append(("SELL", k, self.sell_px[k], self.size))
                room -= 1
        return out

    def on_bar(self, o, h, l, c):
        """Process one candle: sells fill when the high reaches them, rebuys when the low reaches them.
        Conservative ordering: within a bar, a level can complete at most one leg."""
        for side, k, px, q in self.orders():
            if side == "SELL" and h >= px * (1 + self.through) and k not in self.open:
                self.coins -= q
                self.usdt += q * px * (1 - self.fee)
                self.sold_value += q * px
                self.open[k] = px / (1 + self.step)
            elif side == "BUY" and l <= px * (1 - self.through) and k in self.open:
                cost = q * px * (1 + self.fee)
                if self.usdt >= cost:
                    self.usdt -= cost
                    self.coins += q
                    sell = self.sell_px[k]
                    self.profit += q * sell * (1 - self.fee) - cost
                    self.rounds += 1
                    del self.open[k]
        self.min_coins = min(self.min_coins, self.coins)
        if not self.open and c < self.anchor / (1 + self.step) ** self.recenter_steps:
            self.set_anchor(c)


def simulate(candles, **kw):
    """candles: list of (t, o, h, l, c). Returns summary dict."""
    if not candles:
        return {}
    g = Grid(anchor=candles[0][4], **kw)
    for t, o, h, l, c in candles:
        g.on_bar(o, h, l, c)
    last = candles[-1][4]
    start_val = kw["start_coins"] * candles[0][4] + kw["start_usdt"]
    return {"rounds": g.rounds, "profit_usdt": round(g.profit, 2), "coins_end": g.coins, "coins_min": g.min_coins,
            "usdt_end": round(g.usdt, 2), "open_rounds": len(g.open),
            "value_end_usdt": round(g.coins * last + g.usdt, 2), "hold_value_usdt": round(kw["start_coins"] * last + kw["start_usdt"], 2),
            "start_value_usdt": round(start_val, 2)}
