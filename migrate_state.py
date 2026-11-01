"""Back up compatible state upgrades; never change risk rules of old open positions."""

import argparse
import copy
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path


def current_settings():
    from cascade_final4_paper import Final4PaperHunter
    import binance_cascade_hunter_dynamic_v2 as config

    probe = Final4PaperHunter.__new__(Final4PaperHunter)
    probe.strategy = config
    return probe.settings()


def previous_protection_settings():
    return dict(
        mode="final3_scale_trail",
        LEVERAGE=50,
        MAX_POSITION_PCT=0.10,
        CASCADE_THRESHOLD=0.012,
        VOLUME_SPIKE=3.5,
        TAKE_PROFIT_PCT=0.018,
        STOP_LOSS_PCT=0.07,
        COOLDOWN_SEC=8,
        SCAN_INTERVAL=600,
        MAX_SYMBOLS_TO_SCAN=40,
        MAX_CONCURRENT_POSITIONS=5,
        MIN_QUOTE_VOLUME=15_000_000,
        position_basis="wallet_total",
        stop_basis="original_fill_price",
        add_levels=[0.025],
        add_fraction=1.0,
        add_basis="original_fill_notional",
        trail_activation=0.018,
        trail_distance=0.009,
        target_step=0.005,
    )


def old_scale_settings():
    return dict(
        previous_protection_settings(),
        post_add_stop=0.04,
        partial_adverse=0.03,
        partial_fraction=0.5,
        break_even_after_add=True,
    )


def legacy_settings():
    return dict(
        previous_protection_settings(),
        STOP_LOSS_PCT=0.15,
        MAX_CONCURRENT_POSITIONS=3,
        add_levels=[0.05, 0.10],
        add_fraction=0.5,
        stop_basis="original_signal_price",
    )


def previous_final3_settings():
    return dict(previous_protection_settings(), STOP_LOSS_PCT=0.05)


def unfinished_reversal_settings():
    settings = current_settings().copy()
    settings.pop("hedge_positions")
    settings["TAKE_PROFIT_PCT"] = 0.018
    settings["mode"] = "final3_smart_reversal"
    return settings


def previous_reversal_settings():
    settings = current_settings().copy()
    settings["mode"] = "final3_smart_reversal"
    return settings


def plan(path):
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    current = current_settings()
    if state.get("version") != 1:
        raise ValueError("Unknown state version; original preserved")
    if state.get("settings") == current:
        return state, False
    previous = state.get("settings")
    allowed = [
        legacy_settings(),
        previous_final3_settings(),
        previous_protection_settings(),
        old_scale_settings(),
        unfinished_reversal_settings(),
    ]
    allowed.append(previous_reversal_settings())
    if previous not in allowed:
        raise ValueError("Unrecognized state settings; original preserved")
    accounts = state.get("account_positions")
    positions = state.get("strategy_positions")
    if (
        not isinstance(accounts, dict)
        or not isinstance(positions, dict)
        or set(accounts) != set(positions)
    ):
        raise ValueError("Inconsistent positions; original preserved")
    if accounts and previous not in (
        unfinished_reversal_settings(),
        previous_reversal_settings(),
    ):
        raise ValueError(
            "Old scale-in positions are open. Resume the old version or use a NEW --state/session; original preserved"
        )
    wallet = state.get("balance_usdt")
    if (
        not isinstance(wallet, (int, float))
        or not math.isfinite(wallet)
        or not isinstance(state.get("trades"), list)
        or not isinstance(state.get("last_trade"), dict)
    ):
        raise ValueError("Invalid wallet or ledger; original preserved")
    if not accounts:
        for key, value in [
            ("used_usdt", 0),
            ("equity_usdt", wallet),
            ("free_usdt", max(0, wallet)),
        ]:
            if not math.isclose(float(state.get(key, math.nan)), value, abs_tol=1e-8):
                raise ValueError("Inconsistent idle wallet; original preserved")
    state = copy.deepcopy(state)
    for key, fill in state["account_positions"].items():
        p = state["strategy_positions"][key]
        if previous == previous_reversal_settings():
            # Only the repository identity changes; keep both position IDs and links.
            if (
                p.get("symbol", key) != fill.get("symbol", key)
                or p.get("side") != fill.get("side")
                or p.get("amount") != fill.get("amount")
            ):
                raise ValueError("Inconsistent reversal position; original preserved")
            continue
        if (
            p.get("is_reversal")
            or p.get("reversed")
            or fill.get("side") != p.get("side")
        ):
            raise ValueError(
                "Unsupported unfinished reversal state; original preserved"
            )
        if any(
            not isinstance(fill.get(k), (int, float))
            or not math.isfinite(fill[k])
            or fill[k] <= 0
            for k in ["entry", "amount", "margin"]
        ):
            raise ValueError("Invalid position; original preserved")
        fill.update(symbol=key, position_id=key)
        p.update(
            symbol=key,
            position_id=key,
            is_reversal=False,
            parent=None,
            reverse_of=None,
            tp=None,
        )
        p["profit_lock_trigger"] = max(
            (
                t
                for t, l in current["profit_lock_levels"]
                if l > 0 and l <= p.get("profit_locked", 0)
            ),
            default=0,
        )
    state["settings"] = current
    return state, True


def migrate(path):
    path = Path(path)
    state, changed = plan(path)
    if not changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + ".before-risk-update-" + stamp + ".bak")
    shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".migrating")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temp.replace(path)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        _, changed = plan(args.state)
        if not changed:
            print("State already matches current settings.")
            return
        if not args.apply:
            print("Compatible upgrade. Stop all writers, then rerun with --apply.")
            return
        print("Migrated state with backup:", migrate(args.state))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
