"""Deep analysis and optimization of the 30m ORB strategy."""

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from datetime import time
from scripts.optimize_strategy import load_and_precompute, simulate_strategy

n_pc = load_and_precompute("data/historical/NIFTY_5m_180d.json")
b_pc = load_and_precompute("data/historical/BANKNIFTY_5m_180d.json")

print("--- Testing Variations of 30m ORB ---")

# Let's test different Target1 and Trailing parameters, different Scan Windows, and different Stop Losses
variations = [
    # Baseline 30m
    {"name": "30m ORB Base (09:45-13:15, T1=35/70, SL=25/45)", "start": time(9, 45), "end": time(13, 15), "n_sl": 25, "n_t1": 35, "b_sl": 45, "b_t1": 70, "tues": 1.5, "body": 0.40, "bnf_ce": False},
    # Window shifted to 10:00 to let 09:45 candle settle
    {"name": "30m ORB (10:00-13:15, T1=35/70, SL=25/45)", "start": time(10, 0), "end": time(13, 15), "n_sl": 25, "n_t1": 35, "b_sl": 45, "b_t1": 70, "tues": 1.5, "body": 0.40, "bnf_ce": False},
    # Window shifted to 10:15
    {"name": "30m ORB (10:15-13:15, T1=35/70, SL=25/45)", "start": time(10, 15), "end": time(13, 15), "n_sl": 25, "n_t1": 35, "b_sl": 45, "b_t1": 70, "tues": 1.5, "body": 0.40, "bnf_ce": False},
    # Higher Target 1 for better Risk:Reward (NIFTY T1=40 pts, BNF T1=80 pts)
    {"name": "30m ORB (10:00-13:15, Higher T1=40/80, SL=25/45)", "start": time(10, 0), "end": time(13, 15), "n_sl": 25, "n_t1": 40, "b_sl": 45, "b_t1": 80, "tues": 1.5, "body": 0.40, "bnf_ce": False},
    # Higher Target 1 + BNF CE Only
    {"name": "30m ORB (10:00-13:15, T1=40/80, BNF CE Only)", "start": time(10, 0), "end": time(13, 15), "n_sl": 25, "n_t1": 40, "b_sl": 45, "b_t1": 80, "tues": 1.5, "body": 0.40, "bnf_ce": True},
    # T1=45/90 (Runners strategy)
    {"name": "30m ORB (10:00-13:15, Runners T1=45/90, BNF CE Only)", "start": time(10, 0), "end": time(13, 15), "n_sl": 25, "n_t1": 45, "b_sl": 45, "b_t1": 90, "tues": 1.5, "body": 0.40, "bnf_ce": True},
    # T1=35/70 + Tuesday 2.0x (aggressive expiry sizing)
    {"name": "30m ORB (10:00-13:15, T1=35/70, Tuesday 2x Boost)", "start": time(10, 0), "end": time(13, 15), "n_sl": 25, "n_t1": 35, "b_sl": 45, "b_t1": 70, "tues": 2.0, "body": 0.40, "bnf_ce": False},
    # 30m ORB with tighter BNF SL (40 pts) and higher Target (80 pts)
    {"name": "30m ORB (10:00-13:15, BNF SL 40/T1 80, NIFTY SL 25/T1 40)", "start": time(10, 0), "end": time(13, 15), "n_sl": 25, "n_t1": 40, "b_sl": 40, "b_t1": 80, "tues": 1.5, "body": 0.40, "bnf_ce": False},
    # NIFTY ONLY (What if we trade Nifty only?)
    {"name": "30m ORB NIFTY-CENTRIC (3 lots NIFTY, 1 lot BNF)", "start": time(10, 0), "end": time(13, 15), "n_sl": 25, "n_t1": 40, "b_sl": 45, "b_t1": 75, "tues": 1.5, "body": 0.40, "bnf_ce": False, "lots_n": 3, "lots_b": 1},
]

print(f"{'Variation Name':<56} | {'Trades':<6} | {'Win %':<6} | {'PF':<5} | {'Net P&L (₹)':<12} | {'Max DD':<8}")
print("="*100)

for v in variations:
    res = simulate_strategy(
        n_pc,
        b_pc,
        start_time=v["start"],
        end_time=v["end"],
        nifty_sl=v["n_sl"],
        nifty_t1=v["n_t1"],
        bnf_sl=v["b_sl"],
        bnf_t1=v["b_t1"],
        daily_bias=True,
        min_body_ratio=v["body"],
        tuesday_multiplier=v["tues"],
        orb_window_mins=30,
        bnf_ce_only=v["bnf_ce"],
        base_lots_nifty=v.get("lots_n", 2),
        base_lots_bnf=v.get("lots_b", 2),
    )
    print(f"{v['name']:<56} | {res['total_trades']:<6} | {res['win_rate']:<6.1f} | {res['profit_factor']:<5.2f} | {res['total_net']:>12,.2f} | {res['max_drawdown']:>8,.2f}")
    if res['total_net'] > 40000:
        print("  -> Monthly P&L:")
        for m, st in sorted(res['monthly'].items()):
            print(f"     {m}: ₹{st['net_pnl']:>8,.2f} ({st['trades']} trades, {st['win_rate']}%)")
