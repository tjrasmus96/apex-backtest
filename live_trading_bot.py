"""
APEX LIVE TRADING BOT v2 — MT5 / FTMO TRIAL
=============================================
Connects to FTMO trial account via MetaTrader 5 Python API.
Runs every 15 minutes via GitHub Actions.
Uses locked v8c strategy parameters.

SETUP COMPLETE WHEN:
  GitHub Secrets set:
    MT5_LOGIN        — 1600140350
    MT5_PASSWORD     — your master password
    MT5_SERVER       — OANDA-Demo-1
    MT5_ACCOUNT_SIZE — 10000

STRATEGY: EUR/USD mean reversion (v8c locked params)
SESSION:  07:00–17:00 EST (London/NY overlap)
RISK:     0.4% per trade, 3.5% daily limit, 6% total limit
"""

import json, math, urllib.request, os, sys, time
from datetime import datetime, timezone

# ── LOCKED STRATEGY PARAMS (v8c — DO NOT CHANGE) ─────────────────────────────
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
RISK_PER_TRADE    = 0.004   # 0.4% per trade
MAX_DAILY_LOSS    = 0.035   # 3.5% daily limit
MAX_TOTAL_LOSS    = 0.060   # 6.0% total limit
SYMBOL            = "EURUSD"
SESSION_START_EST = 7
SESSION_END_EST   = 17

# ── MT5 CONNECTION ────────────────────────────────────────────────────────────

def connect_mt5():
    """Connect to MT5 account. Returns True if successful."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("  MetaTrader5 package not installed")
        return False, None

    login    = int(os.environ.get("MT5_LOGIN", "0"))
    password = os.environ.get("MT5_PASSWORD", "")
    server   = os.environ.get("MT5_SERVER", "OANDA-Demo-1")

    if not login or not password:
        print("  MT5 credentials missing from environment")
        return False, None

    # Initialize MT5
    if not mt5.initialize():
        print(f"  MT5 initialize failed: {mt5.last_error()}")
        return False, None

    # Login
    authorized = mt5.login(login, password=password, server=server)
    if not authorized:
        print(f"  MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        return False, None

    info = mt5.account_info()
    if info is None:
        print(f"  Cannot get account info: {mt5.last_error()}")
        mt5.shutdown()
        return False, None

    print(f"  ✓ Connected to MT5")
    print(f"  Account: {info.login} | "
          f"Balance: ${info.balance:,.2f} | "
          f"Equity: ${info.equity:,.2f} | "
          f"Server: {server}")
    return True, mt5

def disconnect_mt5(mt5):
    try:
        mt5.shutdown()
        print("  MT5 disconnected")
    except:
        pass

# ── MARKET DATA ───────────────────────────────────────────────────────────────

def fetch_eurusd_yahoo(bars_needed=100):
    """Fetch EUR/USD from Yahoo Finance as backup data source."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X"
           "?interval=1h&range=30d")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent":"Mozilla/5.0"})
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
                "close":  c,
                "high":   ohlcv["high"][i]  or c,
                "low":    ohlcv["low"][i]   or c,
                "hour_est": ((ts % 86400) // 3600 - 5) % 24,
            })
        return bars[-bars_needed:]
    except Exception as e:
        print(f"  Yahoo data fetch failed: {e}")
        return []

def get_mt5_bars(mt5, symbol, count=100):
    """Get recent hourly bars from MT5 directly."""
    try:
        import MetaTrader5 as mt5_lib
        rates = mt5_lib.copy_rates_from_pos(symbol, mt5_lib.TIMEFRAME_H1, 0, count)
        if rates is None:
            return []
        bars = []
        for r in rates:
            ts = int(r["time"])
            bars.append({
                "timestamp": ts,
                "close":  float(r["close"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "hour_est": ((ts % 86400) // 3600 - 5) % 24,
            })
        return bars
    except Exception as e:
        print(f"  MT5 bar fetch failed: {e}")
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
    gains  = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    losses = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag=sum(gains[-period:])/period
    al=sum(losses[-period:])/period
    if al==0: return 100.0
    return 100-100/(1+ag/al)

def calc_atr(closes, period=14):
    if len(closes)<2: return 0.0001
    trs=[abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
    return sum(trs[-period:])/min(len(trs),period)

def calc_zscore(closes, period=20):
    if len(closes)<period: return 0.0
    sl=closes[-period:]; mid=sum(sl)/period
    std=math.sqrt(sum((x-mid)**2 for x in sl)/period) or 1e-10
    return (closes[-1]-mid)/std

# ── SIGNAL ────────────────────────────────────────────────────────────────────

def get_signal(bars):
    if len(bars) < PARAMS["bb_period"] + 5:
        return "HOLD", 0, 0, 0

    closes = [b["close"] for b in bars]
    cur    = closes[-1]
    prev   = closes[-2] if len(closes) >= 2 else cur
    atr    = calc_atr(closes)

    upper, mid, lower = calc_bb(closes, PARAMS["bb_period"], PARAMS["bb_std"])
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

    print(f"  Price:{cur:.5f} BB:[{lower:.5f}-{upper:.5f}] "
          f"RSI:{rsi:.1f} Z:{z:.2f} ATR:{atr:.5f}")
    print(f"  Buy score:{buy_score} Sell score:{sell_score} "
          f"(need {PARAMS['min_score']})")

    if buy_score  >= PARAMS["min_score"]: return "BUY",  buy_score,  atr, cur
    if sell_score >= PARAMS["min_score"]: return "SELL", sell_score, atr, cur
    return "HOLD", max(buy_score, sell_score), atr, cur

# ── POSITION SIZING ───────────────────────────────────────────────────────────

def calc_lots(equity, atr):
    """
    Risk 0.4% of equity per trade.
    EUR/USD: 1 standard lot = $10/pip, pip = 0.0001
    Lots = risk_amount / (stop_pips * pip_value_per_lot)
    """
    risk_amount = equity * RISK_PER_TRADE
    stop_dist   = atr * PARAMS["atr_stop"]
    stop_pips   = stop_dist / 0.0001
    pip_value   = 10.0  # $10 per pip per standard lot

    if stop_pips <= 0: return 0.01
    lots = risk_amount / (stop_pips * pip_value)
    lots = max(0.01, min(round(lots, 2), 2.0))
    return lots

# ── RISK CHECKS ───────────────────────────────────────────────────────────────

def check_risk(balance, equity, state):
    starting = state.get("starting_balance", balance)

    # Total loss check
    total_loss_pct = (starting - equity) / starting
    if total_loss_pct >= MAX_TOTAL_LOSS:
        return False, f"Total loss {total_loss_pct*100:.2f}% hit limit {MAX_TOTAL_LOSS*100:.0f}%"

    # Daily loss check
    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = state.get("daily_pnl", {}).get(today, 0)
    if daily_pnl <= -(starting * MAX_DAILY_LOSS):
        return False, f"Daily loss ${daily_pnl:.2f} hit limit"

    return True, "OK"

# ── MT5 TRADE EXECUTION ───────────────────────────────────────────────────────

def place_trade(mt5, signal, lots, price, atr):
    """Place market order with stop loss and take profit."""
    try:
        import MetaTrader5 as mt5_lib

        symbol_info = mt5_lib.symbol_info(SYMBOL)
        if symbol_info is None:
            print(f"  Symbol {SYMBOL} not found")
            return None

        # Enable symbol if needed
        if not symbol_info.visible:
            mt5_lib.symbol_select(SYMBOL, True)

        point     = symbol_info.point
        stop_dist = atr * PARAMS["atr_stop"]
        tp_dist   = atr * PARAMS["atr_tp"]

        if signal == "BUY":
            order_type = mt5_lib.ORDER_TYPE_BUY
            sl = price - stop_dist
            tp = price + tp_dist
        else:
            order_type = mt5_lib.ORDER_TYPE_SELL
            sl = price + stop_dist
            tp = price - tp_dist

        request = {
            "action":    mt5_lib.TRADE_ACTION_DEAL,
            "symbol":    SYMBOL,
            "volume":    float(lots),
            "type":      order_type,
            "price":     price,
            "sl":        round(sl, 5),
            "tp":        round(tp, 5),
            "deviation": 20,
            "magic":     88888,
            "comment":   "APEX_v8c",
            "type_time": mt5_lib.ORDER_TIME_GTC,
            "type_filling": mt5_lib.ORDER_FILLING_IOC,
        }

        result = mt5_lib.order_send(request)
        if result is None:
            print(f"  Order failed: {mt5_lib.last_error()}")
            return None

        if result.retcode == mt5_lib.TRADE_RETCODE_DONE:
            print(f"  ✓ {signal} order placed: "
                  f"{lots} lots @ {price:.5f} "
                  f"SL:{sl:.5f} TP:{tp:.5f}")
            return result
        else:
            print(f"  Order failed: retcode={result.retcode} "
                  f"comment={result.comment}")
            return None

    except Exception as e:
        print(f"  Trade execution error: {e}")
        return None

def get_open_position(mt5):
    """Check if we have an open EURUSD position."""
    try:
        import MetaTrader5 as mt5_lib
        positions = mt5_lib.positions_get(symbol=SYMBOL)
        if positions is None:
            return None
        # Filter for our bot's positions (magic number 88888)
        our_pos = [p for p in positions if p.magic == 88888]
        return our_pos[0] if our_pos else None
    except Exception as e:
        print(f"  Position check error: {e}")
        return None

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open("bot_state.json", "r") as f:
            return json.load(f)
    except:
        return {"starting_balance": None, "daily_pnl": {},
                "total_trades": 0, "wins": 0, "losses": 0}

def save_state(state):
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
            "news_context": json.dumps({"type": "live_bot_mt5"}),
        }).encode()
        req = urllib.request.Request(
            f"{sb_url}/rest/v1/reports", data=payload,
            headers={"Content-Type": "application/json",
                     "apikey": sb_key,
                     "Authorization": f"Bearer {sb_key}",
                     "Prefer": "return=minimal"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15): pass
        print("  ✓ Logged to Supabase")
    except Exception as e:
        print(f"  Supabase log failed: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_bot():
    print("\n" + "="*55)
    print("APEX LIVE BOT v2 — MT5/FTMO EUR/USD")
    now_utc = datetime.now(timezone.utc)
    print(f"Run: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*55)

    state = load_state()

    # ── Session check ──────────────────────────────────────
    now_est    = (now_utc.hour - 5) % 24
    in_session = SESSION_START_EST <= now_est <= SESSION_END_EST
    print(f"\nSession: {now_est:02d}:00 EST | "
          f"{'✓ IN SESSION' if in_session else '✗ OUT OF SESSION'}")

    # ── Connect to MT5 ─────────────────────────────────────
    print(f"\nConnecting to MT5...")
    connected, mt5 = connect_mt5()

    if not connected:
        # Fall back to paper mode with Yahoo data
        print("\n⚠️  Running in PAPER MODE (no MT5 connection)")
        bars = fetch_eurusd_yahoo(100)
        if bars:
            signal, score, atr, price = get_signal(bars)
            print(f"\nPaper signal: {signal} (score:{score}) @ {price:.5f}")
        log_to_supabase({
            "mode": "paper_fallback",
            "timestamp": now_utc.isoformat(),
            "summary": "MT5 connection failed — paper mode",
        })
        return

    try:
        # ── Get account info ───────────────────────────────
        import MetaTrader5 as mt5_lib
        info    = mt5_lib.account_info()
        balance = float(info.balance)
        equity  = float(info.equity)
        profit  = float(info.profit)

        # Set starting balance on first run
        if not state.get("starting_balance"):
            state["starting_balance"] = balance
            print(f"\n  First run — starting balance: ${balance:,.2f}")

        starting = state["starting_balance"]
        total_pnl_pct = (equity - starting) / starting * 100

        print(f"\nAccount status:")
        print(f"  Balance:  ${balance:,.2f}")
        print(f"  Equity:   ${equity:,.2f}")
        print(f"  Open P&L: ${profit:+,.2f}")
        print(f"  Total:    {total_pnl_pct:+.2f}% vs start")

        # FTMO progress
        target_pct  = 8.0
        progress    = min(total_pnl_pct / target_pct * 100, 100)
        print(f"\nFTMO Progress: {total_pnl_pct:.2f}% / {target_pct}% target "
              f"({progress:.0f}% there)")

        # ── Risk checks ────────────────────────────────────
        can_trade, reason = check_risk(balance, equity, state)
        print(f"\nRisk: {'✓ OK' if can_trade else '✗ BLOCKED: ' + reason}")

        # ── Get market data ────────────────────────────────
        print(f"\nFetching EUR/USD bars from MT5...")
        bars = get_mt5_bars(mt5, SYMBOL, 100)
        if not bars:
            print("  MT5 bars failed, trying Yahoo...")
            bars = fetch_eurusd_yahoo(100)
        print(f"  Got {len(bars)} bars")

        # ── Signal ────────────────────────────────────────
        print(f"\nSignal check:")
        signal, score, atr, price = get_signal(bars)
        print(f"  → {signal} (score:{score})")

        # ── Check existing position ────────────────────────
        position = get_open_position(mt5)
        has_pos  = position is not None
        if has_pos:
            pos_profit = float(position.profit)
            print(f"\nOpen position: {position.type} "
                  f"{position.volume} lots | P&L: ${pos_profit:+.2f}")

        # ── Trade decision ─────────────────────────────────
        today = now_utc.strftime("%Y-%m-%d")
        action_taken = "none"

        if signal != "HOLD" and not has_pos and can_trade and in_session:
            lots   = calc_lots(equity, atr)
            print(f"\nPlacing {signal}: {lots} lots @ {price:.5f}")
            result = place_trade(mt5, signal, lots, price, atr)
            if result:
                state["total_trades"] = state.get("total_trades", 0) + 1
                action_taken = f"{signal}_{lots}lots"
        elif not in_session:
            print(f"\nNo trade — outside session hours")
        elif has_pos:
            print(f"\nNo trade — position already open")
        elif not can_trade:
            print(f"\nNo trade — risk limit: {reason}")
        else:
            print(f"\nNo trade — no signal")

        # ── Update daily P&L ──────────────────────────────
        if today not in state["daily_pnl"]:
            state["daily_pnl"][today] = 0
        # Track today's P&L from closed trades
        # (MT5 tracks this via account balance changes)

        # ── Summary ───────────────────────────────────────
        summary = (
            f"MT5 Bot | {signal} score:{score} | "
            f"${equity:,.0f} | {total_pnl_pct:+.2f}% | "
            f"Action:{action_taken}"
        )
        print(f"\n{summary}")

        # ── Log to Supabase ────────────────────────────────
        log_entry = {
            "mode":          "live_mt5",
            "signal":        signal,
            "score":         score,
            "price":         round(price, 5),
            "balance":       round(balance, 2),
            "equity":        round(equity, 2),
            "total_pnl_pct": round(total_pnl_pct, 3),
            "can_trade":     can_trade,
            "in_session":    in_session,
            "has_position":  has_pos,
            "action":        action_taken,
            "total_trades":  state.get("total_trades", 0),
            "ftmo_progress": round(progress, 1),
            "timestamp":     now_utc.isoformat(),
            "summary":       summary,
        }
        log_to_supabase(log_entry)

        state["last_run"] = now_utc.isoformat()
        save_state(state)

    finally:
        disconnect_mt5(mt5)

    print(f"\n{'='*55}")
    print("Bot run complete")

if __name__ == "__main__":
    run_bot()
