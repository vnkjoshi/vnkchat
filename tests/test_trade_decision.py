# import pytest
# from datetime import date
# from logic.trade_decision import decide_trade

# TODAY = date(2025, 5, 8)

# @pytest.mark.parametrize("status,ltp,entry_thr,entry_pct,last_entry,wap,pt_type,pt_val,sl_type,sl_val,re_cfg,re_thr,last_buy,expected", [
#     # ENTRY ↑
#     ("Waiting", 110, 100, 10, None, None, "", 0, "", 0, {}, None, None, "BUY"),
#     # ENTRY ↓
#     ("Waiting", 90, 100, -10, None, None, "", 0, "", 0, {}, None, None, "BUY"),
#     # EXIT profit hit (percent)
#     ("Running", 120, None, 0, None, 100, "percentage", 20, "percentage", 0, {}, None, None, "SELL"),
#     # EXIT stop-loss hit (absolute)
#     ("Running", 80, None, 0, None, 100, "absolute", 0, "absolute", 15, {}, None, None, "SELL"),
#     # RE-ENTRY prev_day ↑
#     ("Running", 105, None, 0, date(2025,5,7), 100, "", 0, "", 0,
#      {"prev_day": {"percentage": 5}}, 100, None, "RE-ENTRY"),
#     # RE-ENTRY last_buy ↓
#     ("Running", 90, None, 0, date(2025,5,7), 100, "", 0, "", 0,
#      {"last_buy": {"percentage": -10}}, None, 100, "RE-ENTRY"),
#     # NONE case
#     ("Waiting", 95, 100, 10, None, None, "", 0, "", 0, {}, None, None, "NONE"),
# ])
# def test_decide_trade(status, ltp, entry_thr, entry_pct, last_entry, wap,
#                       pt_type, pt_val, sl_type, sl_val, re_cfg,
#                       re_thr, last_buy, expected):
#     decision = decide_trade(
#         status=status,
#         live_ltp=ltp,
#         today=TODAY,
#         entry_threshold=entry_thr,
#         entry_percentage=entry_pct,
#         last_entry_date=last_entry,
#         weighted_avg_price=wap,
#         profit_target_type=pt_type,
#         profit_target_value=pt_val,
#         stop_loss_type=sl_type,
#         stop_loss_value=sl_val,
#         last_trade_date=None,
#         reentry_params=re_cfg,
#         reentry_threshold=re_thr,
#         last_buy_price=last_buy,
#     )
#     assert decision == expected
