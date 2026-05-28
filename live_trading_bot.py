"""
APEX LIVE TRADING BOT v3 — FTMO WEB API (LINUX COMPATIBLE)
===========================================================
No MetaTrader5 library needed — works on Linux/GitHub Actions.

HOW IT WORKS:
  - Fetches EUR/USD price data from Yahoo Finance
  - Calculates signal using locked v8c parameters
  - Logs signals and would-be trades to Supabase
  - Connects to FTMO via their web trading API

NOTE ON FTMO EXECUTION:
  FTMO's platform (OANDA-Demo-1) uses MT5 which requires Windows
  for direct API trading. For Linux-based execution we have two options:
  
  OPTION A (Current — Paper Signal Mode):
    Bot runs, calculates signals, logs everything to Supabase.
    You manually confirm trades on FTMO web platform.
    Best for the 14-day free trial period.
    
  OPTION B (Full Auto — coming next):
    Connect to a cTrader broker (IC Markets/Pepperstone) which
    has a Linux-compatible REST API. Then enter FTMO challenge
    on a cTrader-based account.

For now this runs Option A — full signal logging so you can
see exactly what the bot would trade, verify it matches the
backtest, then decide on the best execution path.
"""

import json, math, urllib.request, os
from datetime import datetime, timezone

# ── LOCKED STRATEGY PARAMS (v8c) ─────────────────────────────────────────────
PARAMS = {
    "bb_period":  10,
    "bb_std":     2.0,
    "rsi_period": 7,
    "rsi_ob":     65,
    "rsi_os":     40,
    "atr_stop":   2.0,
    "atr_tp":     1.5,
    "min_score":  3,
}
RISK_PER_TRADE    = 0.004
MAX_DAILY_LOSS    = 0.035
MAX_TOTAL_LOSS    = 0.060
SYMBOL            = "EURUSD"
SESSION_START_EST = 7
SESSION_END_EST   = 17
ACCOUNT_SIZE      = float(os.environ.get("MT5_ACCOUNT_SIZE", "10000"))

# ── MARKET DATA ───────────────────────────────────────────────────────────────

def fetch_eurusd(bars_needed=100):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X"
           "?interval=1h&range=30d")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"})
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
                "close":     c,
                "high":      ohlcv["high"][i] or c,
                "low":       ohlcv["low"][i]  or c,
                "hour_est":  ((ts % 86400) // 3600 - 5) % 24,
            })
        print(f"  EUR/USD: {len(bars)} bars fetched")
        return bars[-bars_needed:]
    except Exception as e:
        print(f"  Data fetch failed: {e}")
        return []

# ── INDICATORS ────────────────────────────────────────────────────────────────

def calc_bb(closes, period, std_mult):
    if len(closes) < period:
        c = closes[-1]; return c*1.002, c, c*0.998
    sl  = closes[-period:]; mid = sum(sl)/period
    std = math.sqrt(sum((x-mid)**2 for x in sl)/period) or 1e-10
    return mid+std_mult*std, mid, mid-std_mult*std

def calc_rsi(closes, period):
    if len(closes) < period+1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    if al == 0: return 100.0
    return 100-100/(1+ag/al)

def calc_atr(closes, period=14):
    if len(closes) < 2: return 0.0001
    trs = [abs(closes[i]-closes[i-1]) for i in range(1, len(closes))]
    return sum(trs[-period:])/min(len(trs), period)

def calc_zscore(closes, period=20):
    if len(closes) < period: return 0.0
    sl = closes[-period:]; mid = sum(sl)/period
    std = math.sqrt(sum((x-mid)**2 for x in sl)/period) or 1e-10
    return (closes[-1]-mid)/std

# ── SIGNAL ────────────────────────────────────────────────────────────────────

def get_signal(bars):
    if len(bars) < PARAMS["bb_period"] + 5:
        return "HOLD", 0, 0, 0

    closes = [b["close"] for b in bars]
    cur    = closes[-1]
    prev   = closes[-2] if len(closes) >= 2 else cur
    atr    = calc_atr(closes)

    upper, mid, lower = calc_bb(
        closes, PARAMS["bb_period"], PARAMS["bb_std"])
    rsi = calc_rsi(closes, PARAMS["rsi_period"])
    z   = calc_zscore(closes, PARAMS["bb_period"])

    buy_score = 0
    if cur < lower:            buy_score += 1
    if rsi < PARAMS["rsi_os"]: buy_score += 1
    if z < -1.0:               buy_score += 1
    if cur > prev:             buy_score += 1

    sell_score = 0
    if cur > upper:            sell_score += 1
    if rsi > PARAMS["rsi_ob"]: sell_score += 1
    if z > 1.0:                sell_score += 1
    if cur < prev:             sell_score += 1

    print(f"  Price:  {cur:.5f}")
    print(f"  BB:     {lower:.5f} — {upper:.5f} (mid:{mid:.5f})")
    print(f"  RSI:    {rsi:.1f} | Z-score: {z:.2f} | ATR: {atr:.5f}")
    print(f"  Scores: BUY={buy_score} SELL={sell_score} "
          f"(need {PARAMS['min_score']})")

    if buy_score  >= PARAMS["min_score"]:
        return "BUY",  buy_score,  atr, cur
    if sell_score >= PARAMS["min_score"]:
        return "SELL", sell_score, atr, cur
    return "HOLD", max(buy_score, sell_score), atr, cur

# ── POSITION SIZING ───────────────────────────────────────────────────────────

def calc_lots(equity, atr):
    risk_amount = equity * RISK_PER_TRADE
    stop_pips   = (atr * PARAMS["atr_stop"]) / 0.0001
    pip_value   = 10.0
    if stop_pips <= 0: return 0.01
    lots = risk_amount / (stop_pips * pip_value)
    return max(0.01, min(round(lots, 2), 2.0))

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open("bot_state.json", "r") as f:
            return json.load(f)
    except:
        return {
            "starting_balance": ACCOUNT_SIZE,
            "current_balance":  ACCOUNT_SIZE,
            "daily_pnl":        {},
            "open_trade":       None,
            "total_trades":     0,
            "wins":             0,
            "losses":           0,
            "total_pnl":        0.0,
            "signal_log":       [],
        }

def save_state(state):
    # Keep signal log to last 100 entries
    if len(state.get("signal_log", [])) > 100:
        state["signal_log"] = state["signal_log"][-100:]
    with open("bot_state.json", "w") as f:
        json.dump(state, f, indent=2, default=str)

def log_to_supabase(entry):
    try:
        sb_url = os.environ.get("SUPABASE_URL")
        sb_key = os.environ.get("SUPABASE_KEY")
        if not sb_url or not sb_key: return
        payload = json.dumps({
            "week_ending":  datetime.now(timezone.utc).isoformat(),
            "report_text":  entry.get("summary", ""),
            "bot_data":     entry,
            "news_context": json.dumps({"type": "live_bot_v3"}),
        }).encode()
        req = urllib.request.Request(
            f"{sb_url}/rest/v1/reports", data=payload,
            headers={
                "Content-Type":  "application/json",
                "apikey":        sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Prefer":        "return=minimal",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=15): pass
        print("  ✓ Logged to Supabase")
    except Exception as e:
        print(f"  Supabase log failed: {e}")

# ── PAPER TRADE SIMULATION ────────────────────────────────────────────────────

def simulate_trade_outcome(state, signal, price, atr, current_price=None):
    """
    Simulate open trade outcome for paper trading.
    Checks if existing paper trade hit stop or TP.
    """
    open_trade = state.get("open_trade")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if open_trade and current_price:
        entry   = open_trade["entry"]
        side    = open_trade["side"]
        stop    = open_trade["stop"]
        tp      = open_trade["tp"]
        lots    = open_trade["lots"]
        pip_val = 10.0

        hit_stop = ((side == "BUY"  and current_price <= stop) or
                    (side == "SELL" and current_price >= stop))
        hit_tp   = ((side == "BUY"  and current_price >= tp) or
                    (side == "SELL" and current_price <= tp))

        if hit_stop or hit_tp:
            if side == "BUY":
                pnl = (current_price - entry) / 0.0001 * pip_val * lots
            else:
                pnl = (entry - current_price) / 0.0001 * pip_val * lots

            result = "TP ✓" if hit_tp else "Stop ✗"
            print(f"\n  Paper trade closed: {result}")
            print(f"  {side} {lots}lots @ {entry:.5f} → {current_price:.5f}")
            print(f"  P&L: ${pnl:+.2f}")

            state["current_balance"] = state.get(
                "current_balance", ACCOUNT_SIZE) + pnl
            state["total_pnl"]   = state.get("total_pnl", 0) + pnl
            state["total_trades"] = state.get("total_trades", 0) + 1
            if pnl > 0: state["wins"]   = state.get("wins", 0) + 1
            else:       state["losses"] = state.get("losses", 0) + 1

            if today not in state["daily_pnl"]:
                state["daily_pnl"][today] = 0
            state["daily_pnl"][today] += pnl
            state["open_trade"] = None

    return state

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_bot():
    now_utc = datetime.now(timezone.utc)
    print("\n" + "="*55)
    print("APEX LIVE BOT v3 — EUR/USD SIGNAL MONITOR")
    print(f"Run: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*55)

    state = load_state()

    # ── Session check ──
    now_est    = (now_utc.hour - 5) % 24
    in_session = SESSION_START_EST <= now_est <= SESSION_END_EST
    print(f"\nSession: {now_est:02d}:00 EST | "
          f"{'✓ IN SESSION' if in_session else '✗ OUT OF SESSION'}")

    # ── Fetch data ──
    print(f"\nFetching EUR/USD data...")
    bars = fetch_eurusd(100)
    if not bars:
        print("No data available. Exiting.")
        return

    # ── Check paper trade outcome ──
    current_price = bars[-1]["close"] if bars else None
    state = simulate_trade_outcome(
        state, None, None, None, current_price)

    # ── Calculate signal ──
    print(f"\nSignal calculation:")
    signal, score, atr, price = get_signal(bars)
    print(f"\n  → SIGNAL: {signal} (score: {score}/4)")

    # ── Account status ──
    balance    = state.get("current_balance", ACCOUNT_SIZE)
    starting   = state.get("starting_balance", ACCOUNT_SIZE)
    total_pnl  = state.get("total_pnl", 0)
    total_pct  = (balance - starting) / starting * 100
    wins       = state.get("wins", 0)
    losses     = state.get("losses", 0)
    trades     = state.get("total_trades", 0)
    wr         = wins/trades*100 if trades > 0 else 0
    ftmo_prog  = min(total_pct / 8.0 * 100, 100) if total_pct > 0 else 0

    print(f"\nPaper Account Status:")
    print(f"  Balance:      ${balance:,.2f}")
    print(f"  Total P&L:    ${total_pnl:+,.2f} ({total_pct:+.2f}%)")
    print(f"  Trades:       {trades} ({wins}W / {losses}L, WR:{wr:.0f}%)")
    print(f"  FTMO Progress:{ftmo_prog:.0f}% toward 8% target")

    # ── Risk check ──
    today     = now_utc.strftime("%Y-%m-%d")
    daily_pnl = state.get("daily_pnl", {}).get(today, 0)
    daily_pct = daily_pnl / starting * 100
    total_loss_pct = (starting - balance) / starting * 100

    can_trade = (total_loss_pct < MAX_TOTAL_LOSS * 100 and
                 daily_pnl > -(starting * MAX_DAILY_LOSS))

    print(f"\nRisk limits:")
    print(f"  Today P&L:    ${daily_pnl:+.2f} ({daily_pct:+.2f}%) "
          f"| limit: -{MAX_DAILY_LOSS*100:.1f}%")
    print(f"  Total loss:   {total_loss_pct:.2f}% "
          f"| limit: {MAX_TOTAL_LOSS*100:.1f}%")
    print(f"  Can trade:    {'✓ YES' if can_trade else '✗ NO'}")

    # ── Paper trade entry ──
    open_trade   = state.get("open_trade")
    has_position = open_trade is not None
    action       = "none"

    if has_position:
        ot = open_trade
        print(f"\nOpen paper trade: {ot['side']} {ot['lots']}lots "
              f"@ {ot['entry']:.5f} | "
              f"SL:{ot['stop']:.5f} TP:{ot['tp']:.5f}")

    if (signal != "HOLD" and not has_position
            and can_trade and in_session):
        lots      = calc_lots(balance, atr)
        stop_dist = atr * PARAMS["atr_stop"]
        tp_dist   = atr * PARAMS["atr_tp"]
        stop      = price - stop_dist if signal=="BUY" else price + stop_dist
        tp        = price + tp_dist   if signal=="BUY" else price - tp_dist
        stop_pips = stop_dist / 0.0001
        tp_pips   = tp_dist   / 0.0001
        risk_usd  = balance * RISK_PER_TRADE

        print(f"\n{'='*55}")
        print(f"PAPER TRADE SIGNAL:")
        print(f"  Action:   {signal}")
        print(f"  Entry:    {price:.5f}")
        print(f"  Stop:     {stop:.5f} ({stop_pips:.1f} pips)")
        print(f"  Target:   {tp:.5f} ({tp_pips:.1f} pips)")
        print(f"  Size:     {lots} lots")
        print(f"  Risk:     ${risk_usd:.2f} ({RISK_PER_TRADE*100:.1f}%)")
        print(f"  R:R:      1:{PARAMS['atr_tp']/PARAMS['atr_stop']:.1f}")
        print(f"{'='*55}")
        print(f"\n⚡ ACTION REQUIRED ON FTMO PLATFORM:")
        print(f"  1. Open MT5 or FTMO web trader")
        print(f"  2. {signal} EURUSD — {lots} lots")
        print(f"  3. Set Stop Loss:   {stop:.5f}")
        print(f"  4. Set Take Profit: {tp:.5f}")

        state["open_trade"] = {
            "side":       signal,
            "entry":      price,
            "stop":       stop,
            "tp":         tp,
            "lots":       lots,
            "entry_time": now_utc.isoformat(),
        }
        action = f"{signal}_{lots}lots"

    elif not in_session:
        print(f"\nNo trade — outside session hours")
    elif has_position:
        print(f"\nMonitoring open trade")
    elif not can_trade:
        print(f"\nNo trade — risk limit hit")
    else:
        print(f"\nNo signal — waiting for setup")

    # ── Log signal ──
    log_entry = {
        "timestamp":    now_utc.isoformat(),
        "signal":       signal,
        "score":        score,
        "price":        round(price, 5),
        "in_session":   in_session,
        "can_trade":    can_trade,
        "has_position": has_position,
        "action":       action,
    }
    state.setdefault("signal_log", []).append(log_entry)

    # ── Save and log ──
    state["last_run"] = now_utc.isoformat()
    save_state(state)

    summary = (
        f"Bot v3 | {signal} score:{score} | "
        f"${balance:,.0f} ({total_pct:+.2f}%) | "
        f"FTMO:{ftmo_prog:.0f}% | Action:{action}"
    )
    log_to_supabase({
        "mode":          "paper_signal",
        "signal":        signal,
        "score":         score,
        "price":         round(price, 5),
        "balance":       round(balance, 2),
        "total_pct":     round(total_pct, 3),
        "ftmo_progress": round(ftmo_prog, 1),
        "trades":        trades,
        "win_rate":      round(wr, 1),
        "action":        action,
        "timestamp":     now_utc.isoformat(),
        "summary":       summary,
    })

    print(f"\n{'='*55}")
    print(f"Next run: in ~15 minutes (automated)")
    print(f"View logs: Supabase dashboard")

if __name__ == "__main__":
    run_bot()
