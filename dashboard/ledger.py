"""Round trips include all partial exits, final exits and fees."""

import math


def round_trips(fills):
    opened, closed, unmatched = {}, [], []
    for fill in fills:
        symbol = fill["symbol"]
        position_id = fill.get("position_id") or symbol
        amount, price = float(fill["amount"]), float(fill["price"])
        if not fill.get("reduceOnly"):
            already_open = position_id in opened
            if position_id not in opened:
                opened[position_id] = {
                    "symbol": symbol,
                    "position_id": position_id,
                    "side": fill["side"],
                    "entry_time": fill.get("timestamp"),
                    "fills": [],
                    "amount": 0.0,
                    "remaining_amount": 0.0,
                    "cost": 0.0,
                    "fees_usdt": 0.0,
                    "gross_pnl_usdt": 0.0,
                    "adds": 0,
                    "partials": 0,
                    "history_complete": not bool(
                        fill.get("scale_in") or fill.get("scaleIn")
                    ),
                    "entry_reason": fill.get("entry_reason"),
                    "entry_signal": fill.get("entry_signal"),
                    "strength": fill.get("strength"),
                    "margin_pct": fill.get("margin_pct"),
                }
            p = opened[position_id]
            old_remaining = p["remaining_amount"]
            p["average_entry"] = (
                p.get("average_entry", 0) * old_remaining + price * amount
            ) / (old_remaining + amount)
            p["remaining_amount"] += amount
            p["amount"] += amount
            p["cost"] += amount * price
            p["adds"] += int(already_open)
        else:
            p = opened.get(position_id)
            if p is None:
                unmatched.append(fill)
                continue
            if amount > p["remaining_amount"] and not math.isclose(
                amount, p["remaining_amount"], rel_tol=1e-8
            ):
                p["history_complete"] = False
            p["remaining_amount"] = max(0.0, p["remaining_amount"] - amount)
            p["gross_pnl_usdt"] += float(fill.get("pnl_usdt", 0))
            p["exit_time"], p["exit_price"] = fill.get("timestamp"), price
            p["exit_reason"] = fill.get("exit_reason")
            if fill.get("partial_close") or p["remaining_amount"] > 1e-9:
                p["partials"] += 1
            else:
                p["remaining_amount"] = 0.0
                opened.pop(position_id)
                closed.append(p)
        p["fees_usdt"] += float(fill.get("fee_usdt", 0))
        p["net_pnl_usdt"] = p["gross_pnl_usdt"] - p["fees_usdt"]
        p["fills"].append(fill)
    return list(opened.values()), closed
