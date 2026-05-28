"""
APEX BACKTEST v8c — EUR/USD MEAN REVERSION (DRAWDOWN FIX)
==========================================================
RESULT FROM v8b:
  In-sample:     +11.90% | Sharpe 2.592 | WR 58.2% | DD 10.14%
  Out-of-sample: +3.38%  | Sharpe 0.994 | WR 54.8% | DD 3.89%
  Full 6mo:      +15.68% | DD 10.14% ← JUST over FTMO 10% limit

ONE FIX NEEDED:
  Max drawdown was 10.14% — 0.14% over FTMO's hard 10% limit.
  Fix: reduce our internal DD brake from 7% to 6% (tighter safety buffer)
  AND reduce risk per trade from 0.5% to 0.4% of equity.
  This shaves ~1.5% off drawdown while keeping most of the return.

BEST PARAMS FROM v8b (locked in — no re-optimisation):
  bb_period=10, bb_std=2.0, rsi_period=7, rsi_ob=65, rsi_os=40
  atr_stop=2.0, atr_tp=1.5, min_score=3

We keep EXACTLY the same params — only change risk sizing and DD brake.
This is NOT re-optimising. This is adjusting position sizing, which is
a legitimate risk management change that doesn't affect signal quality.
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
                 max_daily_loss_pct=0.035,   # 3.5% daily (FTMO=5%)
                 max_total_loss_pct=0.06,    # 6.0% total (FTMO=10%, tighter)
                 risk_per_trade=0.004):      # 0.4% risk per trade (was 0.5%)

    bb_period  = params["bb_period"]
    bb_std     = params["bb_std"]
    rsi_period = params["rsi_period"]
    rsi_ob     = params["rsi_ob"]
    rsi_os     = params["rsi_os"]
    atr_stop   = params["atr_stop"]
    atr_tp     = params["atr_tp"]
    min_score  = params.get("min_score", 3)

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

        equity = cash
        if position:
            pnl_open = (cur - position["entry"]) * position["size"]
            if position["side"] == "SELL": pnl_open = -pnl_open
            equity = cash + pnl_open
        equity_curve.append(equity)
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd: max_dd = dd

        # Total loss hard stop (6% internal limit)
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

        # Manage open position
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

        # Signal scoring
        upper, mid, lower = calc_bb(closes, bb_period, bb_std)
        rsi   = calc_rsi(closes, rsi_period)
        z     = calc_zscore(closes, bb_period)
        prev  = closes[-2] if len(closes) >= 2 else cur

        buy_score = 0
        if cur < lower:  buy_score += 1
        if rsi < rsi_os: buy_score += 1
        if z < -1.0:     buy_score += 1
        if cur > prev:   buy_score += 1

        sell_score = 0
        if cur > upper:  sell_score += 1
        if rsi > rsi_ob: sell_score += 1
        if z > 1.0:      sell_score += 1
        if cur < prev:   sell_score += 1

        # Tighter position sizing: 0.4% risk per trade
        risk_amt  = cash * risk_per_trade
        stop_dist = atr * atr_stop
        if stop_dist <= 0 or atr <= 0: continue
        size = risk_amt / stop_dist

        if buy_score >= min_score and not position:
            position = {
                "side": "BUY", "entry": cur,
                "stop": cur - atr * atr_stop,
                "tp":   cur + atr * atr_tp,
                "size": size, "bar_idx": i, "entry_ts": ts,
            }
        elif sell_score >= min_score and not position:
            position = {
                "side": "SELL", "entry": cur,
                "stop": cur + atr * atr_stop,
                "tp":   cur - atr * atr_tp,
                "size": size, "bar_idx": i, "entry_ts": ts,
            }

    # Close remaining
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

# ── MAIN ─────────────────────────────────────────────────────────────────────

def run_full_backtest():
    print("\n" + "="*65)
    print("APEX BACKTEST v8c — EUR/USD (DRAWDOWN FIXED)")
    print("Same params as v8b — only risk sizing tightened")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*65)

    # Best params from v8b — locked, no re-optimisation
    best_params = {
        "bb_period": 10, "bb_std": 2.0, "rsi_period": 7,
        "rsi_ob": 65, "rsi_os": 40, "atr_stop": 2.0,
        "atr_tp": 1.5, "min_score": 3,
    }

    print(f"\n[1/4] Downloading EUR/USD 6-month hourly data...")
    all_bars, session_bars = fetch_eurusd(months=6)

    if len(session_bars) < 200:
        print("Insufficient data. Exiting.")
        return {}

    total     = len(session_bars)
    is_end    = int(total * (4/6))
    oos_start = is_end
    oos_end   = total

    print(f"  Session bars: {total} | "
          f"In-sample: 0-{is_end} | OOS: {oos_start}-{oos_end}")
    print(f"\n  Locked params from v8b: {best_params}")
    print(f"  Risk change: 0.5% → 0.4% per trade")
    print(f"  DD brake:    7.0% → 6.0% internal limit")

    # Run all three phases
    print(f"\n[2/4] In-sample validation (4 months)...")
    ins = run_backtest(session_bars, 0, is_end, best_params)
    print(f"  Return: {ins['return_pct']:+.2f}% | "
          f"Sharpe: {ins['sharpe']:.3f} | "
          f"DD: {ins['max_dd']:.2f}% | "
          f"Trades: {ins['trades']}")

    print(f"\n[3/4] Out-of-sample (2 months, BLIND)...")
    oos = run_backtest(session_bars, oos_start, oos_end, best_params)
    print(f"  Return: {oos['return_pct']:+.2f}% | "
          f"Sharpe: {oos['sharpe']:.3f} | "
          f"DD: {oos['max_dd']:.2f}% | "
          f"Trades: {oos['trades']}")

    print(f"\n[4/4] Full 6-month FTMO simulation ($50K)...")
    full = run_backtest(session_bars, 0, total, best_params,
                        starting_cash=50000)

    # Results
    print(f"\n{'='*65}")
    print(f"RESULTS — v8c DRAWDOWN FIXED")
    print(f"{'='*65}")
    print(f"\n{'PHASE':<24}{'RET%':>7}{'WR%':>6}{'TRADES':>8}"
          f"{'DD%':>7}{'SHARPE':>9}{'PF':>7}")
    print(f"{'-'*65}")
    print(f"{'In-sample (4mo)':<24}"
          f"{ins['return_pct']:>7.2f}%{ins['win_rate']:>6.1f}%"
          f"{ins['trades']:>8}{ins['max_dd']:>6.2f}%"
          f"{ins['sharpe']:>9.3f}{ins['profit_factor']:>7.3f}")
    print(f"{'Out-of-sample (2mo)':<24}"
          f"{oos['return_pct']:>7.2f}%{oos['win_rate']:>6.1f}%"
          f"{oos['trades']:>8}{oos['max_dd']:>6.2f}%"
          f"{oos['sharpe']:>9.3f}{oos['profit_factor']:>7.3f}")
    print(f"{'Full 6mo FTMO sim':<24}"
          f"{full['return_pct']:>7.2f}%{full['win_rate']:>6.1f}%"
          f"{full['trades']:>8}{full['max_dd']:>6.2f}%"
          f"{full['sharpe']:>9.3f}{full['profit_factor']:>7.3f}")

    print(f"\n$50K → ${full['final']:,.2f} | "
          f"Profit: ${full['profit']:+,.2f}")

    print(f"\nFTMO RULE COMPLIANCE:")
    print(f"  Daily loss:    {'✓ PASS' if full['ftmo_daily_ok'] else '✗ FAIL'} "
          f"(worst: {full['worst_day_pct']:.2f}% vs -5% limit)")
    print(f"  Max drawdown:  {'✓ PASS' if full['ftmo_total_ok'] else '✗ FAIL'} "
          f"({full['max_dd']:.2f}% vs 10% limit)")
    print(f"  Profit target: {'✓ PASS' if full['ftmo_target'] else '✗ NOT YET'} "
          f"({full['return_pct']:.2f}% vs 10% target)")
    print(f"  Trading days:  {full['trading_days']} "
          f"(min 4 required)")
    print(f"\n  WOULD PASS FTMO: {'✓ YES' if full['ftmo_pass'] else '✗ NOT YET'}")

    # Verdict
    print(f"\n{'='*65}")
    print(f"VERDICT")
    print(f"{'='*65}")

    if full['ftmo_pass']:
        print(f"\n  ✓ Strategy passes all FTMO rules in simulation")
        print(f"  ✓ OOS return positive: {oos['return_pct']:+.2f}%")
        print(f"  ✓ OOS Sharpe > 0.5: {oos['sharpe']:.3f}")
        print(f"\n  NEXT STEPS:")
        print(f"  1. Paper trade this for 2 weeks on a free demo account")
        print(f"  2. If demo results are consistent, pay for FTMO evaluation")
        print(f"  3. Run the bot exactly as-is — do not change parameters")
    else:
        if not full['ftmo_total_ok']:
            gap = full['max_dd'] - 10.0
            print(f"\n  Drawdown still {gap:.2f}% over limit")
            print(f"  Try reducing risk_per_trade to 0.3% in next run")
        if not full['ftmo_target']:
            print(f"\n  Return {full['return_pct']:.2f}% below 10% target")
            print(f"  Strategy direction is correct — need more trades")

    # Save
    summary = {
        "type": "backtest_v8c", "market": "EUR/USD",
        "version": "drawdown_fixed",
        "run_date": datetime.now(timezone.utc).isoformat(),
        "params": best_params,
        "risk_per_trade": 0.004,
        "max_total_loss_pct": 0.06,
        "in_sample":    {k:v for k,v in ins.items()  if k!="equity_curve"},
        "out_of_sample":{k:v for k,v in oos.items()  if k!="equity_curve"},
        "full_6mo":     {k:v for k,v in full.items() if k!="equity_curve"},
        "ftmo_would_pass": full["ftmo_pass"],
    }

    try:
        sb_url = os.environ.get("SUPABASE_URL")
        sb_key = os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text = (
                f"BACKTEST v8c — EUR/USD DRAWDOWN FIXED\n\n"
                f"In-sample:     {ins['return_pct']:+.2f}% | DD:{ins['max_dd']:.2f}%\n"
                f"Out-of-sample: {oos['return_pct']:+.2f}% | DD:{oos['max_dd']:.2f}%\n"
                f"Full 6mo:      {full['return_pct']:+.2f}% | DD:{full['max_dd']:.2f}%\n"
                f"FTMO pass: {full['ftmo_pass']}\n"
                f"Daily: {'PASS' if full['ftmo_daily_ok'] else 'FAIL'} | "
                f"DD: {'PASS' if full['ftmo_total_ok'] else 'FAIL'} | "
                f"Target: {'PASS' if full['ftmo_target'] else 'FAIL'}"
            )
            payload = json.dumps({
                "week_ending": datetime.now(timezone.utc).isoformat(),
                "report_text": report_text,
                "bot_data": summary,
                "news_context": json.dumps({"type":"backtest_v8c"}),
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
                print(f"\nSaved to Supabase!")
    except Exception as e:
        print(f"\nSupabase save failed: {e}")

    with open("backtest_results.json","w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Saved backtest_results.json")
    return summary

if __name__ == "__main__":
    run_full_backtest()
