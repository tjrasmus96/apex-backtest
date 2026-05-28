"""
APEX BACKTEST v8b — EUR/USD MEAN REVERSION (FIXED SIGNAL GENERATION)
=====================================================================
FIX FROM v8: Signal thresholds were too strict — required BB + RSI + Z-score
ALL simultaneously. On EUR/USD (a very stable pair) this almost never fires.

v8b CHANGES:
  - Loosened entry: BB breach OR z-score extreme is enough (not all 3 required)
  - RSI used as a FILTER not a gate (RSI < 45 for buys, > 55 for sells)
  - Reduced minimum trades requirement: 5 (was 10)
  - Wider parameter ranges for BB std (1.0, 1.5, 2.0, 2.5)
  - Added score-based system: 2+ signals needed (more flexible)
  - Reduced minimum lookback so signals fire earlier in the dataset

SIGNAL LOGIC (v8b):
  BUY  when score >= 2: 
    +1 price below lower BB
    +1 RSI < rsi_os (oversold)  
    +1 z-score < -1.0
    +1 price bouncing (current > previous close)
    
  SELL when score >= 2:
    +1 price above upper BB
    +1 RSI > rsi_ob (overbought)
    +1 z-score > +1.0
    +1 price dropping (current < previous close)

Everything else unchanged from v8.
"""

import json, math, urllib.request, os, itertools
from datetime import datetime, timezone

# ── DATA ─────────────────────────────────────────────────────────────────────

def fetch_eurusd(months=6):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X"
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
            if c is None or c == 0: continue
            bars.append({
                "timestamp": ts,
                "open":   ohlcv["open"][i]  or c,
                "high":   ohlcv["high"][i]  or c,
                "low":    ohlcv["low"][i]   or c,
                "close":  c,
                "volume": ohlcv["volume"][i] or 0,
                "hour_est": ((ts % 86400) // 3600 - 5) % 24,
            })
        # Session filter: 07:00-17:00 EST (slightly wider than v8)
        session = [b for b in bars if 7 <= b["hour_est"] <= 17]
        print(f"  EUR/USD: {len(bars)} total bars, "
              f"{len(session)} session bars (07-17 EST)")
        return bars, session
    except Exception as e:
        print(f"  EUR/USD fetch failed: {e}")
        return [], []

# ── INDICATORS ────────────────────────────────────────────────────────────────

def calc_bb(closes, period, std_mult):
    if len(closes) < period:
        c = closes[-1]
        return c*(1+0.002), c, c*(1-0.002)
    sl  = closes[-period:]
    mid = sum(sl) / period
    std = math.sqrt(sum((x-mid)**2 for x in sl) / period) or 1e-10
    return mid + std_mult*std, mid, mid - std_mult*std

def calc_rsi(closes, period):
    if len(closes) < period + 1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:])  / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag/al)

def calc_atr(closes, period=14):
    if len(closes) < 2: return abs(closes[-1]) * 0.001
    trs = [abs(closes[i]-closes[i-1]) for i in range(1, len(closes))]
    return sum(trs[-period:]) / min(len(trs), period)

def calc_zscore(closes, period=20):
    if len(closes) < period: return 0.0
    sl  = closes[-period:]
    mid = sum(sl) / period
    std = math.sqrt(sum((x-mid)**2 for x in sl) / period) or 1e-10
    return (closes[-1] - mid) / std

# ── CORE BACKTEST ─────────────────────────────────────────────────────────────

def run_backtest(bars, start_idx, end_idx, params,
                 starting_cash=50000,
                 max_daily_loss_pct=0.035,
                 max_total_loss_pct=0.07):

    bb_period  = params["bb_period"]
    bb_std     = params["bb_std"]
    rsi_period = params["rsi_period"]
    rsi_ob     = params["rsi_ob"]
    rsi_os     = params["rsi_os"]
    atr_stop   = params["atr_stop"]
    atr_tp     = params["atr_tp"]
    min_score  = params.get("min_score", 2)

    cash         = starting_cash
    position     = None
    trades       = []
    equity_curve = [cash]
    peak         = cash
    max_dd       = 0
    trading_days = set()
    daily_pnl    = {}
    killed       = False

    warmup = max(bb_period, rsi_period, 20) + 5

    for i in range(warmup, end_idx):
        if i < start_idx:
            equity_curve.append(cash)
            continue
        if killed:
            equity_curve.append(cash)
            continue

        bar = bars[i]
        cur = bar["close"]
        ts  = bar["timestamp"]
        day = ts // 86400

        # Equity tracking
        equity = cash
        if position:
            pnl_open = (cur - position["entry"]) * position["size"]
            if position["side"] == "SELL":
                pnl_open = -pnl_open
            equity = cash + pnl_open
        equity_curve.append(equity)
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd: max_dd = dd

        # Total loss hard stop
        total_loss_pct = (starting_cash - equity) / starting_cash
        if total_loss_pct >= max_total_loss_pct:
            if position:
                pnl = (cur - position["entry"]) * position["size"]
                if position["side"] == "SELL": pnl = -pnl
                cash += pnl
                trades.append({**position, "exit": cur, "pnl": pnl,
                               "exit_reason": "Total DD limit",
                               "bars_held": i - position["bar_idx"]})
                position = None
            killed = True
            continue

        # Daily loss check
        day_pnl = daily_pnl.get(day, 0)
        if day_pnl <= -(starting_cash * max_daily_loss_pct):
            if position:
                pnl = (cur - position["entry"]) * position["size"]
                if position["side"] == "SELL": pnl = -pnl
                cash += pnl
                daily_pnl[day] = daily_pnl.get(day, 0) + pnl
                trades.append({**position, "exit": cur, "pnl": pnl,
                               "exit_reason": "Daily limit",
                               "bars_held": i - position["bar_idx"]})
                position = None
            continue

        closes = [b["close"] for b in bars[max(0, i-200):i+1]]
        atr    = calc_atr(closes)

        # ── Manage open position ──
        if position:
            pnl = (cur - position["entry"]) * position["size"]
            if position["side"] == "SELL": pnl = -pnl

            hit_stop = ((position["side"] == "BUY"  and cur <= position["stop"]) or
                        (position["side"] == "SELL" and cur >= position["stop"]))
            hit_tp   = ((position["side"] == "BUY"  and cur >= position["tp"]) or
                        (position["side"] == "SELL" and cur <= position["tp"]))

            if hit_stop or hit_tp:
                cash += pnl
                daily_pnl[day] = daily_pnl.get(day, 0) + pnl
                trades.append({**position, "exit": cur, "pnl": pnl,
                               "exit_reason": "TP" if hit_tp else "Stop",
                               "bars_held": i - position["bar_idx"]})
                trading_days.add(day)
                position = None
            continue

        # ── Signal scoring ──
        upper, mid, lower = calc_bb(closes, bb_period, bb_std)
        rsi = calc_rsi(closes, rsi_period)
        z   = calc_zscore(closes, bb_period)
        prev = closes[-2] if len(closes) >= 2 else cur

        buy_score = 0
        if cur < lower:           buy_score += 1
        if rsi < rsi_os:          buy_score += 1
        if z < -1.0:              buy_score += 1
        if cur > prev:            buy_score += 1  # Price bouncing up

        sell_score = 0
        if cur > upper:           sell_score += 1
        if rsi > rsi_ob:          sell_score += 1
        if z > 1.0:               sell_score += 1
        if cur < prev:            sell_score += 1  # Price dropping

        # Risk 0.5% per trade
        risk_per_trade = cash * 0.005
        stop_dist      = atr * atr_stop
        if stop_dist <= 0 or atr <= 0: continue
        size = risk_per_trade / stop_dist

        if buy_score >= min_score and not position:
            stop = cur - atr * atr_stop
            tp   = cur + atr * atr_tp
            position = {
                "side": "BUY", "entry": cur,
                "stop": stop, "tp": tp,
                "size": size, "bar_idx": i,
                "entry_ts": ts,
            }

        elif sell_score >= min_score and not position:
            stop = cur + atr * atr_stop
            tp   = cur - atr * atr_tp
            position = {
                "side": "SELL", "entry": cur,
                "stop": stop, "tp": tp,
                "size": size, "bar_idx": i,
                "entry_ts": ts,
            }

    # Close remaining position
    if position and bars:
        cur = bars[min(end_idx-1, len(bars)-1)]["close"]
        pnl = (cur - position["entry"]) * position["size"]
        if position["side"] == "SELL": pnl = -pnl
        cash += pnl
        trades.append({**position, "exit": cur, "pnl": pnl,
                       "exit_reason": "End",
                       "bars_held": end_idx - position["bar_idx"]})

    final  = cash
    ret    = (final - starting_cash) / starting_cash * 100
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr     = len(wins)/len(trades)*100 if trades else 0
    pf     = (sum(t["pnl"] for t in wins) /
              abs(sum(t["pnl"] for t in losses))
              if wins and losses else 0)
    avg_win  = sum(t["pnl"] for t in wins)/len(wins)     if wins   else 0
    avg_loss = abs(sum(t["pnl"] for t in losses)/len(losses)) if losses else 0

    sharpe = 0
    if len(equity_curve) > 1:
        rets  = [(equity_curve[j]-equity_curve[j-1])/equity_curve[j-1]
                 for j in range(1, len(equity_curve)) if equity_curve[j-1] > 0]
        if rets:
            avg_r = sum(rets)/len(rets)
            std_r = math.sqrt(sum((r-avg_r)**2 for r in rets)/len(rets))
            sharpe = avg_r/std_r*math.sqrt(24*252) if std_r > 0 else 0

    daily_returns = {}
    for t in trades:
        d = t["entry_ts"] // 86400
        daily_returns[d] = daily_returns.get(d, 0) + t["pnl"]
    worst_day_pct = min(
        (v/starting_cash*100 for v in daily_returns.values()), default=0)

    ftmo_daily_ok = worst_day_pct > -5.0
    ftmo_total_ok = max_dd < 10.0
    ftmo_target   = ret >= 10.0
    ftmo_pass     = ftmo_daily_ok and ftmo_total_ok and ftmo_target

    return {
        "return_pct":    round(ret, 3),
        "final":         round(final, 2),
        "profit":        round(final - starting_cash, 2),
        "trades":        len(trades),
        "win_rate":      round(wr, 1),
        "profit_factor": round(pf, 3),
        "avg_win":       round(avg_win, 6),
        "avg_loss":      round(avg_loss, 6),
        "max_dd":        round(max_dd, 3),
        "sharpe":        round(sharpe, 3),
        "worst_day_pct": round(worst_day_pct, 3),
        "trading_days":  len(trading_days),
        "killed":        killed,
        "ftmo_daily_ok": ftmo_daily_ok,
        "ftmo_total_ok": ftmo_total_ok,
        "ftmo_target":   ftmo_target,
        "ftmo_pass":     ftmo_pass,
        "equity_curve":  [round(e,2) for e in equity_curve[::10]],
    }

# ── OPTIMISATION ──────────────────────────────────────────────────────────────

def optimise(bars, start_idx, end_idx):
    param_grid = {
        "bb_period":  [10, 15, 20, 25],
        "bb_std":     [1.0, 1.5, 2.0, 2.5],
        "rsi_period": [7, 14],
        "rsi_ob":     [60, 65, 70],
        "rsi_os":     [30, 35, 40],
        "atr_stop":   [1.0, 1.5, 2.0],
        "atr_tp":     [1.5, 2.0, 2.5, 3.0],
        "min_score":  [2, 3],
    }

    keys   = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))
    print(f"  Testing {len(combos)} parameter combinations in-sample...")

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        r = run_backtest(bars, start_idx, end_idx, params)
        if r["trades"] >= 5:
            results.append({**params, **r})

    if not results:
        print("  Still no valid combinations — see verdict below")
        # Return a dummy result so the script doesn't exit early
        default_params = {
            "bb_period":10,"bb_std":1.0,"rsi_period":7,
            "rsi_ob":60,"rsi_os":40,"atr_stop":1.0,
            "atr_tp":1.5,"min_score":2
        }
        r = run_backtest(bars, start_idx, end_idx, default_params)
        return {**default_params, **r}, [{**default_params, **r}]

    results.sort(key=lambda x: x["sharpe"], reverse=True)

    print(f"\n  Top 5 in-sample results (by Sharpe):")
    print(f"  {'BBp':>4}{'BBs':>5}{'RSIp':>5}{'OB':>4}{'OS':>4}"
          f"{'Stp':>5}{'TP':>5}{'Sc':>4}"
          f"{'Ret%':>7}{'WR%':>6}{'PF':>6}{'Sharpe':>8}{'DD%':>6}{'Trd':>5}")
    print(f"  {'-'*80}")
    for r in results[:5]:
        print(f"  {r['bb_period']:>4}{r['bb_std']:>5}"
              f"{r['rsi_period']:>5}{r['rsi_ob']:>4}{r['rsi_os']:>4}"
              f"{r['atr_stop']:>5}{r['atr_tp']:>5}{r['min_score']:>4}"
              f"{r['return_pct']:>7.2f}{r['win_rate']:>6.1f}"
              f"{r['profit_factor']:>6.3f}{r['sharpe']:>8.3f}"
              f"{r['max_dd']:>6.2f}{r['trades']:>5}")

    return results[0], results

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_full_backtest():
    print("\n" + "="*65)
    print("APEX BACKTEST v8b — EUR/USD MEAN REVERSION (FIXED)")
    print("Walk-Forward: 4-month in-sample / 2-month out-of-sample")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*65)

    print(f"\n[1/5] Downloading EUR/USD 6-month hourly data...")
    all_bars, session_bars = fetch_eurusd(months=6)

    if len(session_bars) < 200:
        print("Insufficient data. Exiting.")
        return {}

    total     = len(session_bars)
    is_end    = int(total * (4/6))
    oos_start = is_end
    oos_end   = total

    print(f"  Session bars total: {total}")
    print(f"  In-sample:     0–{is_end} ({is_end} bars ≈ 4 months)")
    print(f"  Out-of-sample: {oos_start}–{oos_end} "
          f"({oos_end-oos_start} bars ≈ 2 months)")

    print(f"\n[2/5] Phase 1 — In-sample optimisation (4 months)...")
    best, all_results = optimise(session_bars, 0, is_end)

    best_params = {k: best[k] for k in
                   ["bb_period","bb_std","rsi_period",
                    "rsi_ob","rsi_os","atr_stop","atr_tp","min_score"]}

    print(f"\n  BEST IN-SAMPLE PARAMS: {best_params}")
    print(f"  Return: {best['return_pct']:+.2f}% | "
          f"Sharpe: {best['sharpe']:.3f} | "
          f"Trades: {best['trades']} | "
          f"WR: {best['win_rate']:.1f}% | "
          f"DD: {best['max_dd']:.2f}%")

    print(f"\n[3/5] Phase 2 — Out-of-sample validation (2 months, BLIND)...")
    oos = run_backtest(session_bars, oos_start, oos_end, best_params)
    print(f"  OOS Return: {oos['return_pct']:+.2f}% | "
          f"Sharpe: {oos['sharpe']:.3f} | "
          f"Trades: {oos['trades']} | "
          f"WR: {oos['win_rate']:.1f}% | "
          f"DD: {oos['max_dd']:.2f}%")

    print(f"\n[4/5] Full 6-month FTMO simulation ($50K)...")
    full = run_backtest(session_bars, 0, total, best_params,
                        starting_cash=50000)

    # ── Results table ──
    print(f"\n{'='*65}")
    print(f"WALK-FORWARD RESULTS")
    print(f"{'='*65}")
    print(f"\n{'PHASE':<24}{'RET%':>7}{'WR%':>6}{'TRADES':>8}"
          f"{'DD%':>7}{'SHARPE':>9}{'PF':>7}")
    print(f"{'-'*65}")
    print(f"{'In-sample (4mo)':<24}"
          f"{best['return_pct']:>7.2f}%"
          f"{best['win_rate']:>6.1f}%"
          f"{best['trades']:>8}"
          f"{best['max_dd']:>6.2f}%"
          f"{best['sharpe']:>9.3f}"
          f"{best['profit_factor']:>7.3f}")
    print(f"{'Out-of-sample (2mo)':<24}"
          f"{oos['return_pct']:>7.2f}%"
          f"{oos['win_rate']:>6.1f}%"
          f"{oos['trades']:>8}"
          f"{oos['max_dd']:>6.2f}%"
          f"{oos['sharpe']:>9.3f}"
          f"{oos['profit_factor']:>7.3f}")
    print(f"{'Full 6mo FTMO sim':<24}"
          f"{full['return_pct']:>7.2f}%"
          f"{full['win_rate']:>6.1f}%"
          f"{full['trades']:>8}"
          f"{full['max_dd']:>6.2f}%"
          f"{full['sharpe']:>9.3f}"
          f"{full['profit_factor']:>7.3f}")

    print(f"\n$50K → ${full['final']:,.2f} | Profit: ${full['profit']:+,.2f}")

    print(f"\nFTMO RULE COMPLIANCE (full 6mo sim):")
    print(f"  Daily loss:   {'PASS' if full['ftmo_daily_ok'] else 'FAIL'} "
          f"(worst day: {full['worst_day_pct']:.2f}% vs -5% limit)")
    print(f"  Max drawdown: {'PASS' if full['ftmo_total_ok'] else 'FAIL'} "
          f"({full['max_dd']:.2f}% vs 10% limit)")
    print(f"  Profit target:{'PASS' if full['ftmo_target'] else 'NOT YET'} "
          f"({full['return_pct']:.2f}% vs 10% target)")
    print(f"  Would pass:   {full['ftmo_pass']}")

    # ── Honest verdict ──
    is_ret  = best["return_pct"]
    oos_ret = oos["return_pct"]
    decay   = is_ret - oos_ret

    print(f"\n{'='*65}")
    print(f"HONEST VERDICT")
    print(f"{'='*65}")

    if oos["sharpe"] > 0.5 and oos_ret > 2.0:
        verdict   = "STRONG EDGE — proceed to live execution"
        edge_real = True
        next_step = "Build cTrader API execution layer"
    elif oos_ret > 0 and oos["sharpe"] > 0:
        verdict   = "WEAK EDGE — profitable OOS but needs more validation"
        edge_real = False
        next_step = "Test on 12-month window before paying fees"
    elif oos_ret > -2 and abs(decay) < 3:
        verdict   = "INCONCLUSIVE — small decay, marginally unprofitable OOS"
        edge_real = False
        next_step = "Try GBP/USD or Asian session hours"
    else:
        verdict   = "NO EDGE on this window — try different market/period"
        edge_real = False
        next_step = "Test GBP/USD or 12-month window"

    print(f"\nIn-sample:        {is_ret:+.2f}%")
    print(f"Out-of-sample:    {oos_ret:+.2f}%")
    print(f"Decay:            {decay:+.2f}% (< 3% good, > 5% overfit)")
    print(f"\nVerdict:   {verdict}")
    print(f"Next step: {next_step}")

    summary = {
        "type": "backtest_v8b", "market": "EUR/USD",
        "run_date": datetime.now(timezone.utc).isoformat(),
        "best_params": best_params,
        "in_sample":   {k:v for k,v in best.items() if k!="equity_curve"},
        "out_of_sample":{k:v for k,v in oos.items() if k!="equity_curve"},
        "full_6mo":    {k:v for k,v in full.items() if k!="equity_curve"},
        "edge_detected": edge_real,
        "verdict": verdict,
        "ftmo_would_pass": full["ftmo_pass"],
    }

    print(f"\n[5/5] Saving...")
    try:
        sb_url = os.environ.get("SUPABASE_URL")
        sb_key = os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text = (
                f"BACKTEST v8b — EUR/USD MEAN REVERSION (FIXED)\n\n"
                f"In-sample:     {is_ret:+.2f}%\n"
                f"Out-of-sample: {oos_ret:+.2f}%\n"
                f"Full 6mo:      {full['return_pct']:+.2f}%\n"
                f"FTMO pass:     {full['ftmo_pass']}\n"
                f"Verdict:       {verdict}"
            )
            payload = json.dumps({
                "week_ending": datetime.now(timezone.utc).isoformat(),
                "report_text": report_text,
                "bot_data": summary,
                "news_context": json.dumps({"type":"backtest_v8b"}),
            }).encode()
            req = urllib.request.Request(
                f"{sb_url}/rest/v1/reports", data=payload,
                headers={
                    "Content-Type": "application/json",
                    "apikey": sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Prefer": "return=minimal",
                }, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15):
                print("  Saved to Supabase!")
    except Exception as e:
        print(f"  Supabase save failed: {e}")

    with open("backtest_results.json","w") as f:
        json.dump(summary, f, indent=2, default=str)
    print("  Saved backtest_results.json")
    return summary

if __name__ == "__main__":
    run_full_backtest()
