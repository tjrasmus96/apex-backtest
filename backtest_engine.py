"""
APEX BACKTEST v8 — EUR/USD MEAN REVERSION (FTMO-OPTIMISED)
===========================================================
STRATEGY: Mean reversion on EUR/USD during London/NY session overlap
MARKET:   Forex (EUR/USD) — lower volatility, tighter spreads than crypto
TIMEFRAME: 1-hour bars
SESSION:  08:00–17:00 EST (London/NY overlap = highest liquidity)

WHY THIS FITS FTMO RULES PERFECTLY:
  - No time limit means we can be patient — 0.15%/day target over 70 days
  - 5% daily loss limit = our bot hard-stops at 3.5% (big safety buffer)
  - 10% max drawdown = our bot hard-stops at 7% (big safety buffer)
  - Mean reversion in ranging forex = consistent small wins, not big swings
  - London/NY overlap = tightest spreads, most liquid, most ranging behaviour

WALK-FORWARD METHODOLOGY (honest):
  In-sample:     months 1-4 (parameter optimisation)
  Out-of-sample: months 5-6 (blind validation — never touched during optimisation)
  ONLY proceed to live trading if OOS Sharpe > 0.5 AND OOS return > 0%

FTMO RISK RULES HARD-CODED:
  Max daily loss:  3.5% (FTMO limit is 5% — we use 3.5% for safety buffer)
  Max total loss:  7.0% (FTMO limit is 10% — we use 7% for safety buffer)
  Min trading days: 4 per phase (we target 1 trade per day minimum)
  
PARAMETERS TESTED:
  bb_period : Bollinger Band period (10, 15, 20, 25)
  bb_std    : Standard deviation multiplier (1.5, 2.0, 2.5)
  rsi_period: RSI period (7, 14, 21)
  rsi_ob    : RSI overbought level (65, 70, 75)
  rsi_os    : RSI oversold level (25, 30, 35)
  atr_stop  : Stop loss ATR multiplier (1.0, 1.5, 2.0)
  atr_tp    : Take profit ATR multiplier (1.5, 2.0, 2.5, 3.0)

DATA SOURCE: Yahoo Finance EUR/USD hourly
"""

import json, math, urllib.request, os, itertools
from datetime import datetime, timezone

# ── DATA ─────────────────────────────────────────────────────────────────────

def fetch_eurusd(months=6):
    """Fetch EUR/USD hourly data from Yahoo Finance"""
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
                # Hour of day in EST (UTC-5, approximate)
                "hour_est": ((ts % 86400) // 3600 - 5) % 24,
            })
        # Filter to trading hours only (08:00-17:00 EST)
        session = [b for b in bars if 8 <= b["hour_est"] <= 17]
        print(f"  EUR/USD: {len(bars)} total bars, "
              f"{len(session)} session bars (08-17 EST)")
        return bars, session
    except Exception as e:
        print(f"  EUR/USD fetch failed: {e}")
        return [], []

# ── INDICATORS ────────────────────────────────────────────────────────────────

def calc_bb(closes, period, std_mult):
    if len(closes) < period:
        c = closes[-1]
        return c*(1+0.001*std_mult), c, c*(1-0.001*std_mult)
    sl  = closes[-period:]
    mid = sum(sl) / period
    std = math.sqrt(sum((x-mid)**2 for x in sl) / period)
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
                 max_daily_loss_pct=0.035,   # 3.5% (FTMO=5%, we use 3.5%)
                 max_total_loss_pct=0.07):    # 7.0% (FTMO=10%, we use 7%)
    """
    Run EUR/USD mean reversion backtest on session bars.
    Only trades during session hours (already filtered in data).
    Hard-coded FTMO risk limits.
    """
    bb_period  = params["bb_period"]
    bb_std     = params["bb_std"]
    rsi_period = params["rsi_period"]
    rsi_ob     = params["rsi_ob"]
    rsi_os     = params["rsi_os"]
    atr_stop   = params["atr_stop"]
    atr_tp     = params["atr_tp"]

    cash          = starting_cash
    position      = None   # {side, entry, stop, tp, size, bar_idx}
    trades        = []
    equity_curve  = [cash]
    peak          = cash
    max_dd        = 0
    trading_days  = set()
    daily_pnl     = {}
    killed        = False   # Hard stop triggered

    for i in range(max(bb_period, rsi_period, 30), end_idx):
        if i < start_idx:
            equity_curve.append(cash)
            continue
        if killed:
            equity_curve.append(cash)
            continue

        bar    = bars[i]
        cur    = bar["close"]
        ts     = bar["timestamp"]
        day    = ts // 86400

        # Track equity
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

        # FTMO total loss check
        total_loss_pct = (starting_cash - equity) / starting_cash
        if total_loss_pct >= max_total_loss_pct:
            # Close position if open, then stop trading
            if position:
                pnl = (cur - position["entry"]) * position["size"]
                if position["side"] == "SELL": pnl = -pnl
                cash += pnl
                trades.append({**position, "exit": cur, "pnl": pnl,
                               "exit_reason": "Total DD limit hit",
                               "bars_held": i - position["bar_idx"]})
                position = None
            killed = True
            continue

        # FTMO daily loss check
        day_pnl = daily_pnl.get(day, 0)
        max_daily_loss = starting_cash * max_daily_loss_pct
        if day_pnl <= -max_daily_loss:
            if position:
                pnl = (cur - position["entry"]) * position["size"]
                if position["side"] == "SELL": pnl = -pnl
                cash += pnl
                daily_pnl[day] = daily_pnl.get(day, 0) + pnl
                trades.append({**position, "exit": cur, "pnl": pnl,
                               "exit_reason": "Daily loss limit hit",
                               "bars_held": i - position["bar_idx"]})
                position = None
            continue

        closes = [b["close"] for b in bars[max(0,i-100):i+1]]
        atr    = calc_atr(closes)

        # ── Manage open position ──
        if position:
            pnl = (cur - position["entry"]) * position["size"]
            if position["side"] == "SELL": pnl = -pnl

            hit_stop = (position["side"] == "BUY"  and cur <= position["stop"]) or \
                       (position["side"] == "SELL" and cur >= position["stop"])
            hit_tp   = (position["side"] == "BUY"  and cur >= position["tp"]) or \
                       (position["side"] == "SELL" and cur <= position["tp"])

            if hit_stop or hit_tp:
                cash += position["size"] * position["entry"] + pnl
                daily_pnl[day] = daily_pnl.get(day, 0) + pnl
                trades.append({**position, "exit": cur, "pnl": pnl,
                               "exit_reason": "TP" if hit_tp else "Stop",
                               "bars_held": i - position["bar_idx"]})
                trading_days.add(day)
                position = None
            continue

        # ── Signal generation ──
        upper, mid, lower = calc_bb(closes, bb_period, bb_std)
        rsi = calc_rsi(closes, rsi_period)
        z   = calc_zscore(closes, bb_period)

        # Position sizing: risk 0.5% of account per trade
        # (conservative — FTMO rewards consistency over aggression)
        risk_per_trade = cash * 0.005
        stop_distance  = atr * atr_stop
        if stop_distance <= 0: continue
        # In forex, size = units of base currency
        # For simplicity, size = risk_dollars / stop_distance
        size = risk_per_trade / stop_distance

        # BUY signal: price below lower BB + RSI oversold + z-score negative
        if (cur < lower and rsi < rsi_os and z < -1.5):
            stop = cur - atr * atr_stop
            tp   = cur + atr * atr_tp
            position = {
                "side": "BUY", "entry": cur,
                "stop": stop, "tp": tp,
                "size": size, "bar_idx": i,
                "entry_ts": ts,
            }
            cash -= size * cur  # simplified: reserve cost basis

        # SELL signal: price above upper BB + RSI overbought + z-score positive
        elif (cur > upper and rsi > rsi_ob and z > 1.5):
            stop = cur + atr * atr_stop
            tp   = cur - atr * atr_tp
            position = {
                "side": "SELL", "entry": cur,
                "stop": stop, "tp": tp,
                "size": size, "bar_idx": i,
                "entry_ts": ts,
            }
            cash -= size * cur  # simplified: reserve

    # Close any open position at end
    if position and bars:
        cur = bars[min(end_idx-1, len(bars)-1)]["close"]
        pnl = (cur - position["entry"]) * position["size"]
        if position["side"] == "SELL": pnl = -pnl
        cash += position["size"] * position["entry"] + pnl
        trades.append({**position, "exit": cur, "pnl": pnl,
                       "exit_reason": "End", "bars_held": end_idx - position["bar_idx"]})

    # Results
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

    # Sharpe (hourly, annualised)
    sharpe = 0
    if len(equity_curve) > 1:
        rets  = [(equity_curve[i]-equity_curve[i-1])/equity_curve[i-1]
                 for i in range(1, len(equity_curve)) if equity_curve[i-1] > 0]
        if rets:
            avg_r = sum(rets)/len(rets)
            std_r = math.sqrt(sum((r-avg_r)**2 for r in rets)/len(rets))
            sharpe = avg_r/std_r*math.sqrt(24*252) if std_r > 0 else 0

    # FTMO-specific metrics
    daily_returns = {}
    for t in trades:
        d = t["entry_ts"] // 86400
        daily_returns[d] = daily_returns.get(d, 0) + t["pnl"]
    worst_day_pct = min(
        (v/starting_cash*100 for v in daily_returns.values()),
        default=0
    )
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
        "avg_win":       round(avg_win, 4),
        "avg_loss":      round(avg_loss, 4),
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
        "bb_std":     [1.5, 2.0, 2.5],
        "rsi_period": [7, 14, 21],
        "rsi_ob":     [65, 70, 75],
        "rsi_os":     [25, 30, 35],
        "atr_stop":   [1.0, 1.5, 2.0],
        "atr_tp":     [1.5, 2.0, 2.5, 3.0],
    }

    keys   = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))
    print(f"  Testing {len(combos)} parameter combinations in-sample...")

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        r = run_backtest(bars, start_idx, end_idx, params)
        # Only keep results with enough trades to be meaningful
        if r["trades"] >= 10:
            results.append({**params, **r})

    if not results:
        print("  No valid combinations found (too few trades)")
        return None, []

    # Sort by Sharpe — most robust metric
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    print(f"\n  Top 5 in-sample results (by Sharpe):")
    print(f"  {'BBp':>4}{'BBs':>5}{'RSIp':>5}{'OB':>4}{'OS':>4}"
          f"{'Stp':>5}{'TP':>5}{'Ret%':>7}{'WR%':>6}"
          f"{'PF':>6}{'Sharpe':>8}{'DD%':>6}{'Trd':>5}")
    print(f"  {'-'*75}")
    for r in results[:5]:
        print(f"  {r['bb_period']:>4}{r['bb_std']:>5}"
              f"{r['rsi_period']:>5}{r['rsi_ob']:>4}{r['rsi_os']:>4}"
              f"{r['atr_stop']:>5}{r['atr_tp']:>5}"
              f"{r['return_pct']:>7.2f}{r['win_rate']:>6.1f}"
              f"{r['profit_factor']:>6.3f}{r['sharpe']:>8.3f}"
              f"{r['max_dd']:>6.2f}{r['trades']:>5}")

    return results[0], results

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_full_backtest():
    print("\n" + "="*65)
    print("APEX BACKTEST v8 — EUR/USD MEAN REVERSION (FTMO-OPTIMISED)")
    print("Walk-Forward: 4-month in-sample / 2-month out-of-sample")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*65)

    # ── Download data ──
    print(f"\n[1/5] Downloading EUR/USD 6-month hourly data...")
    all_bars, session_bars = fetch_eurusd(months=6)

    if len(session_bars) < 500:
        print("Insufficient data. Exiting.")
        return {}

    total = len(session_bars)
    # 4mo in-sample, 2mo out-of-sample
    is_end    = int(total * (4/6))
    oos_start = is_end
    oos_end   = total

    print(f"  Session bars total: {total}")
    print(f"  In-sample:     0–{is_end} ({is_end} bars ≈ 4 months)")
    print(f"  Out-of-sample: {oos_start}–{oos_end} "
          f"({oos_end-oos_start} bars ≈ 2 months)")
    print(f"  OOS data NEVER seen during optimisation")

    # ── Phase 1: In-sample optimisation ──
    print(f"\n[2/5] Phase 1 — In-sample optimisation (4 months)...")
    best, all_results = optimise(session_bars, 0, is_end)

    if not best:
        print("Optimisation failed. Exiting.")
        return {}

    best_params = {k: best[k] for k in
                   ["bb_period","bb_std","rsi_period",
                    "rsi_ob","rsi_os","atr_stop","atr_tp"]}

    print(f"\n  BEST IN-SAMPLE PARAMS:")
    for k, v in best_params.items():
        print(f"    {k}: {v}")
    print(f"  Return: {best['return_pct']:+.2f}% | "
          f"Sharpe: {best['sharpe']:.3f} | "
          f"WR: {best['win_rate']:.1f}% | "
          f"PF: {best['profit_factor']:.3f} | "
          f"DD: {best['max_dd']:.2f}%")

    # ── Phase 2: Out-of-sample validation ──
    print(f"\n[3/5] Phase 2 — Out-of-sample validation (2 months, BLIND)...")
    oos = run_backtest(session_bars, oos_start, oos_end, best_params)

    print(f"  OOS Return: {oos['return_pct']:+.2f}% | "
          f"Sharpe: {oos['sharpe']:.3f} | "
          f"WR: {oos['win_rate']:.1f}% | "
          f"Trades: {oos['trades']} | "
          f"DD: {oos['max_dd']:.2f}%")

    # ── Phase 3: Full 6-month simulation ──
    print(f"\n[4/5] Full 6-month FTMO simulation ($50K account)...")
    full = run_backtest(session_bars, 0, total, best_params,
                        starting_cash=50000)

    # ── VERDICT ──
    print(f"\n{'='*65}")
    print(f"WALK-FORWARD RESULTS")
    print(f"{'='*65}")
    print(f"\nBest params: BB({best_params['bb_period']},{best_params['bb_std']}) | "
          f"RSI({best_params['rsi_period']},{best_params['rsi_ob']}/{best_params['rsi_os']}) | "
          f"ATR stop={best_params['atr_stop']}x tp={best_params['atr_tp']}x")
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

    print(f"\n$50K account simulation:")
    print(f"  $50,000 → ${full['final']:,.2f} | "
          f"Profit: ${full['profit']:+,.2f}")
    print(f"  Worst day: {full['worst_day_pct']:.2f}% "
          f"(FTMO limit: -5.0%)")
    print(f"  Max drawdown: {full['max_dd']:.2f}% "
          f"(FTMO limit: 10.0%)")
    print(f"  Trading days: {full['trading_days']} "
          f"(FTMO minimum: 4)")
    print(f"  Bot killed early: {'YES' if full['killed'] else 'NO'}")

    # ── HONEST VERDICT ──
    print(f"\n{'='*65}")
    print(f"HONEST VERDICT")
    print(f"{'='*65}")

    is_ret  = best["return_pct"]
    oos_ret = oos["return_pct"]
    decay   = is_ret - oos_ret

    if oos["sharpe"] > 0.5 and oos_ret > 2.0:
        verdict   = "STRONG EDGE — OOS positive with good Sharpe. Proceed."
        edge_real = True
        next_step = "Build live execution layer and paper trade for 2 weeks"
    elif oos_ret > 0 and oos["sharpe"] > 0:
        verdict   = "WEAK EDGE — OOS profitable but needs more validation"
        edge_real = False
        next_step = "Test on additional time windows before risking fees"
    elif abs(decay) < 2 and oos_ret > -1:
        verdict   = "INCONCLUSIVE — small decay but not profitable OOS"
        edge_real = False
        next_step = "Try different session (Asian session, different pair)"
    else:
        verdict   = "NO EDGE — significant decay from IS to OOS"
        edge_real = False
        next_step = "Try GBP/USD or different time window"

    print(f"\nIn-sample return:     {is_ret:+.2f}%")
    print(f"Out-of-sample return: {oos_ret:+.2f}%")
    print(f"Performance decay:    {decay:+.2f}% "
          f"(< 3% = good, > 5% = likely overfit)")
    print(f"\nFTMO rule compliance (full sim):")
    print(f"  Daily loss limit: {'PASS' if full['ftmo_daily_ok'] else 'FAIL'} "
          f"(worst day: {full['worst_day_pct']:.2f}%)")
    print(f"  Total drawdown:   {'PASS' if full['ftmo_total_ok'] else 'FAIL'} "
          f"(max DD: {full['max_dd']:.2f}%)")
    print(f"  Profit target:    {'PASS' if full['ftmo_target'] else 'NOT YET'} "
          f"(return: {full['return_pct']:.2f}%)")
    print(f"\nVerdict:    {verdict}")
    print(f"Next step:  {next_step}")

    # ── Save ──
    summary = {
        "type":          "backtest_v8_eurusd_meanrev",
        "methodology":   "walk_forward_4mo_in_2mo_oos",
        "market":        "EUR/USD forex",
        "strategy":      "Mean reversion London/NY session",
        "run_date":      datetime.now(timezone.utc).isoformat(),
        "best_params":   best_params,
        "in_sample":     {k:v for k,v in best.items()
                          if k not in ("equity_curve",)},
        "out_of_sample": {k:v for k,v in oos.items()
                          if k not in ("equity_curve",)},
        "full_6mo":      {k:v for k,v in full.items()
                          if k not in ("equity_curve",)},
        "edge_detected": edge_real,
        "verdict":       verdict,
        "next_step":     next_step,
        "ftmo_50k_sim":  {
            "start":        50000,
            "end":          full["final"],
            "profit":       full["profit"],
            "daily_ok":     full["ftmo_daily_ok"],
            "drawdown_ok":  full["ftmo_total_ok"],
            "target_hit":   full["ftmo_target"],
            "would_pass":   full["ftmo_pass"],
        }
    }

    print(f"\n[5/5] Saving...")
    try:
        sb_url = os.environ.get("SUPABASE_URL")
        sb_key = os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text = (
                f"BACKTEST v8 — EUR/USD MEAN REVERSION\n"
                f"FTMO $50K Challenge Simulation\n\n"
                f"PARAMS: BB({best_params['bb_period']},{best_params['bb_std']}) "
                f"RSI({best_params['rsi_period']}) "
                f"ATR stop={best_params['atr_stop']}x tp={best_params['atr_tp']}x\n\n"
                f"RESULTS:\n"
                f"In-sample (4mo):     {is_ret:+.2f}% | "
                f"Sharpe: {best['sharpe']:.3f}\n"
                f"Out-of-sample (2mo): {oos_ret:+.2f}% | "
                f"Sharpe: {oos['sharpe']:.3f}\n"
                f"Full 6mo ($50K):     {full['return_pct']:+.2f}% | "
                f"DD: {full['max_dd']:.2f}%\n\n"
                f"FTMO COMPLIANCE:\n"
                f"Daily loss: {'PASS' if full['ftmo_daily_ok'] else 'FAIL'}\n"
                f"Max DD:     {'PASS' if full['ftmo_total_ok'] else 'FAIL'}\n"
                f"Target:     {'PASS' if full['ftmo_target'] else 'NOT YET'}\n"
                f"Would pass: {full['ftmo_pass']}\n\n"
                f"VERDICT: {verdict}"
            )
            payload = json.dumps({
                "week_ending":  datetime.now(timezone.utc).isoformat(),
                "report_text":  report_text,
                "bot_data":     summary,
                "news_context": json.dumps({"type": "backtest_v8"}),
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
    run_full_backtest()
