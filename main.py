"""Sanji's Pump.fun Replay Sprint bot — agent-fun/replay/v2, Python 3.13 stdlib only.

Strategy (own design, deterministic):
- Watch every new coin from its CreateEvent. Buy once per coin, 0.05 SOL,
  only when its early tape shows real buying: at least 2 buy trades from at
  least 2 distinct buyers, at least 1 SOL net inflow, price at least 5%
  above its create price, creator has not sold, and real SOL reserves are
  0.5-80 SOL.
- Sell the whole position on +25% take-profit, -15% stop-loss, after 1200
  slots held, or after 600 slots with no trade (stale). No shorts, no
  leverage, at most 5 open positions, at most 16 orders per batch.
- All decisions are integer math on lamports/reserves (no floats), no clock,
  no randomness, no environment, no network — two runs give byte-identical
  replies. stdout carries replies only.

hello / batch / orders / slot are the protocol words: hello gives capital and
caps, each batch carries events, marks, fills and account at its slot, and
each reply echoes the batch seq with its orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

SOL = makefun_bot.LAMPORTS_PER_SOL
STAKE = SOL // 20          # 0.05 SOL per coin
MIN_BUY = SOL // 100       # 0.01 SOL, the round-trip minimum
MIN_REAL_SOL = SOL // 2    # 0.5 SOL
MAX_REAL_SOL = SOL * 80    # 80 SOL
MIN_AGE_SLOTS = 5
MIN_BUYS = 2
MIN_BUYERS = 2
MIN_NET_SOL = SOL          # at least 1 SOL net inflow over the early tape
MIN_PRICE_UP_BPS = 500     # price at least 5% above its create price
BUY_SLIPPAGE_BPS = 1500
SELL_SLIPPAGE_BPS = 10000
TAKE_PROFIT_BPS = 12500    # sell at value >= cost * 1.25
STOP_LOSS_BPS = 8500       # sell at value <= cost * 0.85
MAX_HOLD_SLOTS = 1200
STALE_SLOTS = 600


class Tape:
    """Early tape for one mint, built only from batch events in order."""

    __slots__ = (
        "created_slot", "create_q", "create_t", "net_sol", "buys",
        "buyers", "creator", "creator_sold", "last_trade_slot",
    )

    def __init__(self, created_slot, create_q, create_t, creator):
        self.created_slot = created_slot
        self.create_q = create_q
        self.create_t = create_t
        self.net_sol = 0
        self.buys = 0
        self.buyers = set()
        self.creator = creator
        self.creator_sold = False
        self.last_trade_slot = created_slot


tapes = {}
decided = set()
opened_slot = {}


def price_up_enough(curve, tape):
    """True if current price >= create price * (1 + MIN_PRICE_UP_BPS/10000),
    via integer cross-multiplication: vq_now * vt_create * 10000 >=
    vq_create * vt_now * (10000 + MIN_PRICE_UP_BPS)."""
    if tape.create_t == 0 or curve.virtual_token_reserves == 0:
        return False
    return (
        curve.virtual_quote_reserves * tape.create_t * 10000
        >= tape.create_q * curve.virtual_token_reserves * (10000 + MIN_PRICE_UP_BPS)
    )


def position_value_lamports(holding_tokens, curve):
    """Tokens valued at current reserves, integer math."""
    if curve.virtual_token_reserves == 0:
        return 0
    return holding_tokens * curve.virtual_quote_reserves // curve.virtual_token_reserves


def on_batch(state, batch):
    slot = batch["slot"]
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    orders = []

    # Fold this batch's events into our tapes, in stream order.
    for e in batch["events"]:
        mint = e["mint"]
        kind = e["event"]
        if kind == "CreateEvent":
            if mint not in tapes:
                tapes[mint] = Tape(
                    e["slot"],
                    int(e["virtual_quote_reserves"]),
                    int(e["virtual_token_reserves"]),
                    e["creator"],
                )
        elif kind == "TradeEvent":
            tape = tapes.get(mint)
            if tape is None:
                tape = Tape(e["slot"], int(e["virtual_quote_reserves"]),
                            int(e["virtual_token_reserves"]), e["creator"])
                tapes[mint] = tape
            sol = int(e["sol_amount"])
            if e["is_buy"]:
                tape.net_sol += sol
                tape.buys += 1
                tape.buyers.add(e["user"])
            else:
                tape.net_sol -= sol
                if e["user"] == tape.creator:
                    tape.creator_sold = True
            tape.last_trade_slot = e["slot"]
        elif kind == "CompleteEvent":
            decided.add(mint)

    # 1) Exits first: free a position slot before deciding new buys.
    for mint in state.positions:
        if mint not in opened_slot:
            opened_slot[mint] = slot
    for mint in list(opened_slot):
        if mint not in state.positions:
            del opened_slot[mint]
    for mint, holding in list(state.positions.items()):
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        cost = holding.cost_lamports
        if cost <= 0 or holding.token_amount <= 0:
            continue
        value = position_value_lamports(holding.token_amount, curve)
        held_for = slot - opened_slot.get(mint, slot)
        stale_for = slot - tapes[mint].last_trade_slot if mint in tapes else 0
        if (
            value * 10000 >= cost * TAKE_PROFIT_BPS
            or value * 10000 <= cost * STOP_LOSS_BPS
            or held_for >= MAX_HOLD_SLOTS
            or stale_for >= STALE_SLOTS
        ):
            orders.append({
                "side": "sell", "mint": mint,
                "token_amount": holding.token_amount,
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            })

    # 2) Entries: one buy per coin, in batch event order.
    free = state.free_cash_lamports()
    for o in orders:
        free -= tx  # sells also pay the tx fee
    slots_open = caps["max_open_positions"] - state.committed_position_count()
    for e in batch["events"]:
        if len(orders) >= caps["max_orders_per_batch"] or slots_open <= 0:
            break
        mint = e["mint"]
        if mint in decided:
            continue
        tape = tapes.get(mint)
        curve = state.curves.get(mint)
        if tape is None or curve is None or curve.complete:
            continue
        if slot - tape.created_slot < MIN_AGE_SLOTS:
            continue
        if tape.buys < MIN_BUYS or len(tape.buyers) < MIN_BUYERS:
            continue
        if tape.net_sol < MIN_NET_SOL or tape.creator_sold:
            continue
        if not price_up_enough(curve, tape):
            continue
        real_sol = curve.real_quote_reserves
        if real_sol < MIN_REAL_SOL or real_sol > MAX_REAL_SOL:
            continue
        cap = state.max_buy_lamports(mint)
        if cap < MIN_BUY:
            continue
        decided.add(mint)
        amount = cap if cap < STAKE else STAKE
        if free < amount + tx:
            continue
        orders.append({
            "side": "buy", "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BUY_SLIPPAGE_BPS,
        })
        free -= amount + tx
        slots_open -= 1

    return orders


def on_hello(state):
    return None


def on_done(state, done):
    return None


if __name__ == "__main__":
    makefun_bot.run(on_batch, on_hello=on_hello, on_done=on_done)
