"""
APEX BACKTEST v7 — CROSS-SECTIONAL MOMENTUM + WALK-FORWARD VALIDATION
======================================================================
HYPOTHESIS:
  Crypto assets that have outperformed their peers over the past N hours
  continue to outperform over the next M hours (momentum effect).
  We go long the top-ranked assets and avoid (or short) the bottom-ranked.
  This is market-structure based, not indicator-based.

WHY THIS IS DIFFERENT FROM PREVIOUS VERSIONS:
  - Not relying on RSI/BB/EMA combinations (over-fitted before)
  - Based on RELATIVE performance across assets, not absolute price levels
  - Partially market-neutral: if all crypto dumps, we're in the top assets,
    not just "everything is long"
  - Documented in academic literature for equities AND crypto

HONEST METHODOLOGY — WALK-FORWARD VALIDATION:
  Phase 1 — IN-SAMPLE  (months 1-4): Parameter optimisation
  Phase 2 — OUT-OF-SAMPLE (months 5-6): Blind validation
  
  If it only works in-sample: the edge isn't real.
  If it holds out-of-sample: we might have something.
  This is the only honest way to test.

PARAMETERS TESTED:
  lookback  : how many hours to measure momentum (12, 24, 48, 72, 96)
  hold_hrs  : how long to hold each position (4, 8, 12, 24)
  top_n     : how many assets to hold at once (1, 2, 3)
  skip_hrs  : skip most recent N hours to avoid reversal (0, 4, 8)

POSITION SIZING:
  Equal weight across top_n positions
  Max 20% of equity per position
  Stop loss: 3x ATR (wider — momentum needs room to breathe)
  No take profit: hold for hold_hrs then rebalance

SECONDARY STRATEGY — FUNDING RATE PROXY:
  Uses price momentum as a proxy for crowded positioning
  Extreme recent momentum (>8% in 24hrs) = likely crowded = fade signal
  Tested separately to isolate its contribution
"""

import json, math, urllib.request, os, itertools
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────

def fetch_historical(symbol, months=6):
    yf_map = {
        "BTC/USD":"BTC-USD","ETH/USD":"ETH-USD","SOL/USD":"SOL-USD",
        "AVAX/USD":"AVAX-USD","LINK/USD":"LINK-USD","BCH/USD":"BCH-USD",
        "LTC/USD":"LTC-USD","AAVE/USD":"AAVE-USD",
    }
    yf_sym = yf_map.get(symbol, symbol.replace("/","-"))
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
           f"?interval=1h&range={months*30}d")
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv = result["indicators"]["quote"][0]
        bars = []
        for i, ts in enumerate(timestamps):
            c = ohlcv["close"][i]
            if c is None: continue
            bars.append({
                "timestamp": ts,
                "close":  c,
                "high":   ohlcv["high"][i]   or c,
                "low":    ohlcv["low"][i]    or c,
                "volume": ohlcv["volume"][i] or 0,
            })
        return bars
    except Exception as e:
        print(f"  {symbol}: failed — {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def calc_atr(closes, period=14):
    if len(closes) < 2: return closes[-1] * 0.02
    trs = [abs(closes[i]-closes[i-1]) for i in range(1, len(closes))]
    return sum(trs[-period:]) / min(len(trs), period)

def momentum_return(closes, lookback, skip=0):
    """
    Return over [lookback] hours, skipping most recent [skip] hours.
    Skipping recent bars avoids short-term reversal contamination.
    """
    if len(closes) < lookback + skip + 1:
        return 0.0
    end   = len(closes) - 1 - skip
    start = end - lookback
    if start < 0: return 0.0
    if closes[start] <= 0: return 0.0
    return (closes[end] - closes[start]) / closes[start]

# ─────────────────────────────────────────────────────────────────────────────
# CROSS-SECTIONAL MOMENTUM BACKTEST
# ─────────────────────────────────────────────────────────────────────────────

def run_cs_momentum(all_bars, symbols, start_bar, end_bar,
                    lookback, hold_hrs, top_n, skip_hrs,
                    starting_cash=100000, stop_atr_mult=3.0):
    """
    Core engine. Runs cross-sectional momentum over a bar range.
    Returns results dict.
    """
    cash       = starting_cash
    positions  = {}   # sym -> {qty, entry, entry_bar, value}
    trades     = []
    equity_curve = [cash]
    peak       = cash
    max_dd     = 0

    rebalance_every = hold_hrs  # rebalance every hold_hrs bars

    for i in range(max(lookback + skip_hrs + 1, 100), end_bar):
        if i < start_bar:
            equity_curve.append(cash)
            continue

        # ── Equity snapshot ──
        total = cash
        for sym, pos in positions.items():
            bars = all_bars.get(sym, [])
            if i < len(bars):
                total += pos["qty"] * bars[i]["close"]
        equity_curve.append(total)
        if total > peak: peak = total
        dd = (peak - total) / peak * 100
        if dd > max_dd: max_dd = dd

        # ── Manage exits ──
        for sym in list(positions.keys()):
            bars = all_bars.get(sym, [])
            if i >= len(bars): continue
            pos    = positions[sym]
            cur    = bars[i]["close"]
            closes = [b["close"] for b in bars[:i+1]]
            atr    = calc_atr(closes)
            held   = i - pos["entry_bar"]

            # Stop loss
            if cur < pos["entry"] - atr * stop_atr_mult:
                profit = (cur - pos["entry"]) * pos["qty"]
                cash  += cur * pos["qty"]
                trades.append({"symbol":sym,"entry":pos["entry"],"exit":cur,
                    "profit":profit,"bars_held":held,"reason":"Stop loss"})
                del positions[sym]
                continue

            # Time exit: held for hold_hrs bars
            if held >= hold_hrs:
                profit = (cur - pos["entry"]) * pos["qty"]
                cash  += cur * pos["qty"]
                trades.append({"symbol":sym,"entry":pos["entry"],"exit":cur,
                    "profit":profit,"bars_held":held,"reason":"Time exit"})
                del positions[sym]

        # ── Rebalance: rank and enter top N ──
        if i % rebalance_every == 0:
            # Close any remaining positions on rebalance
            for sym in list(positions.keys()):
                bars = all_bars.get(sym, [])
                if i >= len(bars): continue
                pos  = positions[sym]
                cur  = bars[i]["close"]
                held = i - pos["entry_bar"]
                profit = (cur - pos["entry"]) * pos["qty"]
                cash  += cur * pos["qty"]
                trades.append({"symbol":sym,"entry":pos["entry"],"exit":cur,
                    "profit":profit,"bars_held":held,"reason":"Rebalance"})
            positions = {}

            # Rank all symbols by momentum
            scores = {}
            for sym in symbols:
                bars = all_bars.get(sym, [])
                if i >= len(bars): continue
                closes = [b["close"] for b in bars[:i+1]]
                scores[sym] = momentum_return(closes, lookback, skip_hrs)

            if not scores: continue

            # Sort: best momentum first
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

            # Enter top_n positions
            n_to_enter = min(top_n, len(ranked))
            # Only enter if momentum is positive (don't buy falling assets)
            candidates = [(s, sc) for s, sc in ranked[:n_to_enter] if sc > 0]

            if not candidates: continue

            alloc_per = total / len(candidates)
            alloc_per = min(alloc_per, total * 0.20)  # max 20% each

            for sym, score in candidates:
                if cash < 10: break
                bars = all_bars.get(sym, [])
                if i >= len(bars): continue
                cur   = bars[i]["close"]
                spend = min(alloc_per, cash * 0.95)
                if spend < 10: continue
                qty   = spend / cur
                cash -= spend
                positions[sym] = {
                    "qty": qty, "entry": cur,
                    "entry_bar": i, "value": spend,
                    "score": score,
                }

    # Close all remaining
    for sym, pos in list(positions.items()):
        bars = all_bars.get(sym, [])
        if not bars: continue
        cur    = bars[min(end_bar-1, len(bars)-1)]["close"]
        profit = (cur - pos["entry"]) * pos["qty"]
        cash  += cur * pos["qty"]
        trades.append({"symbol":sym,"entry":pos["entry"],"exit":cur,
            "profit":profit,"bars_held":end_bar-pos["entry_bar"],
            "reason":"End"})

    final   = cash
    ret     = (final - starting_cash) / starting_cash * 100
    wins    = [t for t in trades if t["profit"] > 0]
    losses  = [t for t in trades if t["profit"] <= 0]
    wr      = len(wins)/len(trades)*100 if trades else 0
    pf      = (sum(t["profit"] for t in wins) /
               abs(sum(t["profit"] for t in losses))
               if wins and losses else 0)
    avg_win  = sum(t["profit"] for t in wins)/len(wins)     if wins   else 0
    avg_loss = abs(sum(t["profit"] for t in losses)/len(losses)) if losses else 0

    # Sharpe
    if len(equity_curve) > 1:
        rets  = [(equity_curve[i]-equity_curve[i-1])/equity_curve[i-1]
                 for i in range(1,len(equity_curve)) if equity_curve[i-1]>0]
        avg_r = sum(rets)/len(rets) if rets else 0
        std_r = math.sqrt(sum((r-avg_r)**2 for r in rets)/len(rets)) if rets else 1
        sharpe = avg_r/std_r*math.sqrt(24*365) if std_r > 0 else 0
    else:
        sharpe = 0

    return {
        "return_pct":     round(ret, 3),
        "final":          round(final, 2),
        "profit":         round(final - starting_cash, 2),
        "trades":         len(trades),
        "win_rate":       round(wr, 1),
        "profit_factor":  round(pf, 3),
        "avg_win":        round(avg_win, 2),
        "avg_loss":       round(avg_loss, 2),
        "max_dd":         round(max_dd, 2),
        "sharpe":         round(sharpe, 3),
        "equity_curve":   [round(e,2) for e in equity_curve[::20]],
    }

# ─────────────────────────────────────────────────────────────────────────────
# PARAMETER OPTIMISATION (in-sample only)
# ─────────────────────────────────────────────────────────────────────────────

def optimise(all_bars, symbols, is_start, is_end):
    """
    Grid search over parameters on IN-SAMPLE data only.
    Returns best parameter set by Sharpe (not return — avoids overfit to return).
    """
    lookbacks  = [12, 24, 48, 72, 96]
    hold_hrss  = [4, 8, 12, 24]
    top_ns     = [1, 2, 3]
    skip_hrss  = [0, 4, 8]

    results = []
    total_combos = len(lookbacks)*len(hold_hrss)*len(top_ns)*len(skip_hrss)
    print(f"  Testing {total_combos} parameter combinations in-sample...")

    for lookback, hold_hrs, top_n, skip_hrs in itertools.product(
            lookbacks, hold_hrss, top_ns, skip_hrss):
        r = run_cs_momentum(
            all_bars, symbols, is_start, is_end,
            lookback=lookback, hold_hrs=hold_hrs,
            top_n=top_n, skip_hrs=skip_hrs,
        )
        results.append({
            "lookback": lookback, "hold_hrs": hold_hrs,
            "top_n": top_n, "skip_hrs": skip_hrs,
            **r
        })

    # Sort by Sharpe (most robust metric — penalises volatile returns)
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    print(f"\n  Top 5 in-sample parameter sets (by Sharpe):")
    print(f"  {'LB':>4} {'Hold':>5} {'TopN':>5} {'Skip':>5} "
          f"{'Ret%':>7} {'WR%':>6} {'PF':>6} {'Sharpe':>8} {'DD%':>6}")
    print(f"  {'-'*60}")
    for r in results[:5]:
        print(f"  {r['lookback']:>4} {r['hold_hrs']:>5} {r['top_n']:>5} "
              f"{r['skip_hrs']:>5} {r['return_pct']:>7.2f} "
              f"{r['win_rate']:>6.1f} {r['profit_factor']:>6.3f} "
              f"{r['sharpe']:>8.3f} {r['max_dd']:>6.2f}")

    return results[0], results  # Best params, all results

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest():
    print("\n" + "="*60)
    print("APEX BACKTEST v7 — CROSS-SECTIONAL MOMENTUM")
    print("Walk-Forward Validation: 4-month in-sample / 2-month OOS")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*60)

    symbols = [
        "BTC/USD","ETH/USD","SOL/USD","AVAX/USD",
        "LINK/USD","BCH/USD","LTC/USD","AAVE/USD"
    ]

    print(f"\n[1/5] Downloading 6 months of data...")
    all_bars = {}
    for sym in symbols:
        bars = fetch_historical(sym, months=6)
        if bars:
            all_bars[sym] = bars
            print(f"  {sym}: {len(bars)} bars")
    
    if not all_bars:
        print("No data downloaded. Exiting.")
        return {}

    # Find the bar counts
    min_bars = min(len(v) for v in all_bars.values())
    total_bars = min_bars

    # Split: first 4 months in-sample, last 2 months out-of-sample
    # 4 months ≈ 2928 bars (4 * 30 * 24.4), 2 months ≈ 1464 bars
    is_end   = int(total_bars * (4/6))   # end of in-sample
    oos_start = is_end
    oos_end  = total_bars

    is_bars  = is_end
    oos_bars = oos_end - oos_start

    print(f"\n  Total bars: {total_bars}")
    print(f"  In-sample:     bars 0–{is_end} ({is_bars} bars ≈ 4 months)")
    print(f"  Out-of-sample: bars {oos_start}–{oos_end} "
          f"({oos_bars} bars ≈ 2 months)")
    print(f"  OOS data was NEVER seen during optimisation")

    # ── Phase 1: In-sample optimisation ──────────────────────────────────────
    print(f"\n[2/5] Phase 1 — In-sample parameter search (4 months)...")
    best_params, all_is_results = optimise(
        all_bars, list(all_bars.keys()), 0, is_end
    )

    print(f"\n  BEST IN-SAMPLE PARAMS:")
    print(f"  Lookback: {best_params['lookback']}hrs | "
          f"Hold: {best_params['hold_hrs']}hrs | "
          f"Top N: {best_params['top_n']} | "
          f"Skip: {best_params['skip_hrs']}hrs")
    print(f"  In-sample Return: {best_params['return_pct']:+.2f}% | "
          f"Sharpe: {best_params['sharpe']:.3f} | "
          f"PF: {best_params['profit_factor']:.3f} | "
          f"WR: {best_params['win_rate']:.1f}%")

    # ── Phase 2: Out-of-sample validation ────────────────────────────────────
    print(f"\n[3/5] Phase 2 — Out-of-sample validation (2 months, BLIND)...")
    print(f"  Using EXACTLY the params from in-sample — no further tuning")

    oos_result = run_cs_momentum(
        all_bars, list(all_bars.keys()),
        start_bar=oos_start, end_bar=oos_end,
        lookback=best_params["lookback"],
        hold_hrs=best_params["hold_hrs"],
        top_n=best_params["top_n"],
        skip_hrs=best_params["skip_hrs"],
    )

    # ── Phase 3: Full 6-month run with best params ────────────────────────────
    print(f"\n[4/5] Full 6-month run with best params...")
    full_result = run_cs_momentum(
        all_bars, list(all_bars.keys()),
        start_bar=0, end_bar=total_bars,
        lookback=best_params["lookback"],
        hold_hrs=best_params["hold_hrs"],
        top_n=best_params["top_n"],
        skip_hrs=best_params["skip_hrs"],
        starting_cash=300000,
    )

    prop_ready = full_result["return_pct"] >= 10 and full_result["max_dd"] < 10

    # ── VERDICT ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"WALK-FORWARD RESULTS")
    print(f"{'='*60}")
    print(f"\nBest params: lookback={best_params['lookback']}h | "
          f"hold={best_params['hold_hrs']}h | "
          f"top_n={best_params['top_n']} | "
          f"skip={best_params['skip_hrs']}h")
    print(f"\n{'PHASE':<22}{'RETURN':>9}{'WIN%':>7}{'TRADES':>8}"
          f"{'DD%':>7}{'SHARPE':>9}{'PF':>7}")
    print(f"{'-'*62}")
    print(f"{'In-sample (4mo)':<22}"
          f"{best_params['return_pct']:>8.2f}%"
          f"{best_params['win_rate']:>6.1f}%"
          f"{best_params['trades']:>8}"
          f"{best_params['max_dd']:>6.2f}%"
          f"{best_params['sharpe']:>9.3f}"
          f"{best_params['profit_factor']:>7.3f}")
    print(f"{'Out-of-sample (2mo)':<22}"
          f"{oos_result['return_pct']:>8.2f}%"
          f"{oos_result['win_rate']:>6.1f}%"
          f"{oos_result['trades']:>8}"
          f"{oos_result['max_dd']:>6.2f}%"
          f"{oos_result['sharpe']:>9.3f}"
          f"{oos_result['profit_factor']:>7.3f}")
    print(f"{'Full 6-month ($300K)':<22}"
          f"{full_result['return_pct']:>8.2f}%"
          f"{full_result['win_rate']:>6.1f}%"
          f"{full_result['trades']:>8}"
          f"{full_result['max_dd']:>6.2f}%"
          f"{full_result['sharpe']:>9.3f}"
          f"{full_result['profit_factor']:>7.3f}")

    print(f"\n$300K → ${full_result['final']:,.0f} | "
          f"Profit: ${full_result['profit']:+,.0f}")

    # ── HONEST VERDICT ────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"HONEST VERDICT")
    print(f"{'='*60}")

    is_ret  = best_params["return_pct"]
    oos_ret = oos_result["return_pct"]
    decay   = is_ret - oos_ret  # performance decay from IS to OOS

    if oos_result["sharpe"] > 0.5 and oos_ret > 0:
        verdict = "EDGE DETECTED — OOS positive with decent Sharpe"
        edge_real = True
    elif oos_ret > 0 and oos_result["sharpe"] > 0:
        verdict = "WEAK EDGE — OOS profitable but Sharpe too low to trust"
        edge_real = False
    elif abs(decay) < 3 and oos_ret > -2:
        verdict = "INCONCLUSIVE — small decay but OOS not profitable"
        edge_real = False
    else:
        verdict = "NO EDGE — significant decay from IS to OOS (likely overfit)"
        edge_real = False

    print(f"\nIn-sample return:     {is_ret:+.2f}%")
    print(f"Out-of-sample return: {oos_ret:+.2f}%")
    print(f"Performance decay:    {decay:+.2f}% (small = good, large = overfit)")
    print(f"\nVerdict: {verdict}")
    print(f"Prop Firm Ready (full run): "
          f"{'YES' if prop_ready else 'NOT YET'}")

    if edge_real:
        print(f"\nThis strategy has a measurable out-of-sample edge.")
        print(f"Recommended next steps:")
        print(f"  1. Test on a different 6-month window to confirm")
        print(f"  2. Account for trading fees (~0.1% per trade)")
        print(f"  3. If still positive after fees: consider live testing small")
    else:
        print(f"\nThis strategy does NOT show a reliable OOS edge.")
        print(f"Do not risk real capital on these results.")
        print(f"Next: try different hypothesis (funding rate, time-of-day, etc.)")

    # ── Save ──────────────────────────────────────────────────────────────────
    summary = {
        "type": "backtest_v7_cs_momentum",
        "methodology": "walk_forward_4mo_in_2mo_oos",
        "run_date": datetime.now(timezone.utc).isoformat(),
        "best_params": {
            "lookback":  best_params["lookback"],
            "hold_hrs":  best_params["hold_hrs"],
            "top_n":     best_params["top_n"],
            "skip_hrs":  best_params["skip_hrs"],
        },
        "in_sample":     {k:v for k,v in best_params.items()
                         if k not in ("equity_curve",)},
        "out_of_sample": {k:v for k,v in oos_result.items()
                         if k not in ("equity_curve",)},
        "full_6mo":      {k:v for k,v in full_result.items()
                         if k not in ("equity_curve",)},
        "edge_detected": edge_real,
        "verdict":       verdict,
        "prop_firm_ready": prop_ready,
    }

    print(f"\n[5/5] Saving...")
    try:
        sb_url = os.environ.get("SUPABASE_URL")
        sb_key = os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text = (
                f"BACKTEST v7 — CROSS-SECTIONAL MOMENTUM\n"
                f"Walk-Forward: 4mo in-sample / 2mo out-of-sample\n\n"
                f"BEST PARAMS (found in-sample):\n"
                f"  Lookback: {best_params['lookback']}h | "
                f"Hold: {best_params['hold_hrs']}h | "
                f"Top N: {best_params['top_n']} | "
                f"Skip: {best_params['skip_hrs']}h\n\n"
                f"RESULTS:\n"
                f"In-sample (4mo):      {is_ret:+.2f}% | "
                f"Sharpe: {best_params['sharpe']:.3f}\n"
                f"Out-of-sample (2mo):  {oos_ret:+.2f}% | "
                f"Sharpe: {oos_result['sharpe']:.3f}\n"
                f"Full 6mo ($300K):     {full_result['return_pct']:+.2f}% | "
                f"DD: {full_result['max_dd']:.2f}%\n\n"
                f"VERDICT: {verdict}\n"
                f"Edge real: {edge_real}\n"
                f"Prop ready: {prop_ready}"
            )
            payload = json.dumps({
                "week_ending":  datetime.now(timezone.utc).isoformat(),
                "report_text":  report_text,
                "bot_data":     summary,
                "news_context": json.dumps({"type": "backtest_v7"}),
            }).encode()
            req = urllib.request.Request(
                f"{sb_url}/rest/v1/reports", data=payload,
                headers={
                    "Content-Type":  "application/json",
                    "apikey":        sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Prefer":        "return=minimal",
                }, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15):
                print(f"  Saved to Supabase!")
    except Exception as e:
        print(f"  Supabase save failed: {e}")

    with open("backtest_results.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"  Saved backtest_results.json")
    return summary

if __name__ == "__main__":
    run_backtest()
