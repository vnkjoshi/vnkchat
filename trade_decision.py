from datetime import date
from typing import Optional, Dict, Any

def decide_trade(
	status: str,
    live_ltp: float,
    today: date,
    entry_threshold: Optional[float],
    entry_percentage: float,
    last_entry_date: Optional[date],
    weighted_avg_price: Optional[float],
    profit_target_type: str,
    profit_target_value: float,
    stop_loss_type: str,
    stop_loss_value: float,
    last_trade_date: Optional[date],
    reentry_params: Dict[str, Any],
    reentry_threshold: Optional[float],
    last_buy_price: Optional[float],
) -> str:
    """
    Pure decision logic for BUY, SELL, RE-ENTRY or NONE.
    """
    # --- ENTRY ---
    if status == "Waiting" and entry_threshold is not None:
        desired = (
            entry_threshold * (1 + entry_percentage / 100)
            if entry_percentage >= 0
            else entry_threshold * (1 - abs(entry_percentage) / 100)
        )
        if (entry_percentage >= 0 and live_ltp >= desired) or (
            entry_percentage < 0 and live_ltp <= desired
        ):
            return "BUY"

    # --- EXIT ---
    if status == "Running" and weighted_avg_price is not None:
        # only one exit per day handled upstream via last_trade_date
        # profit target
        if profit_target_type.lower() == "percentage":
            target = weighted_avg_price * (1 + abs(profit_target_value) / 100)
        else:
            target = weighted_avg_price + profit_target_value

        # stop-loss
        stop = None
        if stop_loss_value and stop_loss_value > 0:
            if stop_loss_type.lower() == "percentage":
                stop = weighted_avg_price * (1 - abs(stop_loss_value) / 100)
            else:
                stop = weighted_avg_price - stop_loss_value

        if live_ltp >= target or (stop is not None and live_ltp <= stop):
            return "SELL"

    # --- RE-ENTRY ---
    # Upstream must guard pending re-entry vs last_entry_date
    if status == "Running" and reentry_params and last_entry_date != today:
        # prev_day config
        prev_cfg = reentry_params.get("prev_day")
        if prev_cfg and reentry_threshold is not None:
            pct = prev_cfg.get("percentage", 0)
            desired = (
                reentry_threshold * (1 + pct / 100)
                if pct >= 0
                else reentry_threshold * (1 - abs(pct) / 100)
            )
            if (pct >= 0 and live_ltp >= desired) or (pct < 0 and live_ltp <= desired):
                return "RE-ENTRY"

        # last_buy config
        last_buy_cfg = reentry_params.get("last_buy")
        if last_buy_cfg and last_buy_price is not None:
            pct = last_buy_cfg.get("percentage", 0)
            desired = (
                last_buy_price * (1 + pct / 100)
                if pct >= 0
                else last_buy_price * (1 - abs(pct) / 100)
            )
            if (pct >= 0 and live_ltp >= desired) or (pct < 0 and live_ltp <= desired):
                return "RE-ENTRY"

        # weighted_avg config
        wavg_cfg = reentry_params.get("weighted_avg")
        if wavg_cfg and weighted_avg_price is not None:
            pct = wavg_cfg.get("percentage", 0)
            desired = (
                weighted_avg_price * (1 + pct / 100)
                if pct >= 0
                else weighted_avg_price * (1 - abs(pct) / 100)
            )
            if (pct >= 0 and live_ltp >= desired) or (pct < 0 and live_ltp <= desired):
                return "RE-ENTRY"

    return "NONE"