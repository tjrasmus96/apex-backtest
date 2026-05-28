"""
APEX BACKTESTING ENGINE v4 — REGIME-ADAPTIVE (World-Class Approach)
=====================================================================
WHAT THE BEST QUANT FUNDS DO (Two Sigma, Renaissance, top crypto algos):
  1. Detect market regime FIRST, then apply the right strategy for it
  2. Never run a momentum strategy in a ranging market (your v2 problem)
  3. Never run mean reversion in a strong trending market
  4. Size positions dynamically based on signal confidence
  5. Use volatility-adjusted position sizing (ATR-based kelly)
  6. Run a portfolio-level drawdown brake — pause all trading if equity
     drops X% from peak (prop firms care about this most)

KEY UPGRADES FROM v3:
  [v4-1] REGIME ROUTER: Single engine detects regime per symbol per bar
          and routes to the correct strategy automatically
          TRENDING   → TREND strategy
          RANGING    → MEAN_REV strategy
          NEUTRAL    → Reduced size MEAN_REV only
          VOLATILE   → No new entries (risk-off)

  [v4-2] VOLATILITY-ADJUSTED SIZING: Position size = (Account * Risk%) / (ATR * multiplier)
          This is how professional quants size — risk the same $ amount per trade
          regardless of price level. Replaces flat % of equity.

  [v4-3] PORTFOLIO DRAWDOWN BRAKE: If combined equity drops 6% from peak,
          ALL strategies pause new entries until recovery to 4% DD.
          Prop firms use 8-10% max — this keeps you well inside.

  [v4-4] FUNDING RATE SIGNAL: Crypto-specific edge. High positive funding =
          longs paying shorts = crowded long = fade momentum (extra mean rev signal)
          Simulated from price momentum as proxy (real version uses exchange API)

  [v4-5] CROSS-ASSET CONFIRMATION: BTC regime used as macro filter.
          If BTC is in downtrend, reduce position sizes on all alts by 50%
          (BTC leads the market — this is a free alpha signal)

  [v4-6] DYNAMIC TAKE PROFIT: TP scales with ADX strength
          Weak trend (ADX 25-30): 2.5x ATR TP (lock in quickly)
          Strong trend (ADX 30-40): 4x ATR TP
          Very strong (ADX 40+): trailing stop (let it run)

TARGET: +10% return, <10% drawdown, Sharpe > 1.0
"""

import json, math, urllib.request, os
from datetime import datetime, timezone

# ── DATA FETCHING ─────────────────────────────────────────────────────────────

def fetch_historical(symbol, months=6):
    yf_map = {
        "BTC/USD":"BTC-USD","ETH/USD":"ETH-USD","SOL/USD":"SOL-USD",
        "AVAX/USD":"AVAX-USD","LINK/USD":"LINK-USD","BCH/USD":"BCH-USD",
        "LTC/USD":"LTC-USD","AAVE/USD":"AAVE-USD","UNI/USD":"UNI-USD",
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
                "open":   ohlcv["open"][i]   or c,
                "high":   ohlcv["high"][i]   or c,
                "low":    ohlcv["low"][i]    or c,
                "close":  c,
                "volume": ohlcv["volume"][i] or 0,
            })
        print(f"  {symbol}: {len(bars)} bars")
        return bars
    except Exception as e:
        print(f"  {symbol}: failed — {e}")
        return []

# ── INDICATORS ────────────────────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:])  / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag/al)

def calc_ema(closes, period):
    if len(closes) < period: return closes[-1]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]: ema = p*k + ema*(1-k)
    return ema

def calc_bb(closes, period=20):
    if len(closes) < period:
        c = closes[-1]; return c*1.02, c, c*0.98
    sl = closes[-period:]; mid = sum(sl)/period
    std = math.sqrt(sum((x-mid)**2 for x in sl)/period)
    return mid+2*std, mid, mid-2*std

def calc_adx(closes, period=14):
    if len(closes) < period*2: return 20.0
    tr   = [abs(closes[i]-closes[i-1]) for i in range(1, len(closes))]
    atr  = sum(tr[-period:])/period or 1
    dm_p = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    dm_m = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    di_p = sum(dm_p[-period:])/period/atr*100
    di_m = sum(dm_m[-period:])/period/atr*100
    if di_p+di_m == 0: return 20.0
    return abs(di_p-di_m)/(di_p+di_m)*100

def calc_atr(closes, period=14):
    if len(closes) < 2: return closes[-1]*0.02
    trs = [abs(closes[i]-closes[i-1]) for i in range(1, len(closes))]
    return sum(trs[-period:]) / min(len(trs), period)

def calc_zscore(closes, period=20):
    if len(closes) < period: return 0.0
    sl = closes[-period:]; mid = sum(sl)/period
    std = math.sqrt(sum((x-mid)**2 for x in sl)/period) or 1
    return (closes[-1]-mid)/std

def calc_stoch_rsi(closes, period=14):
    if len(closes) < period*2: return 50.0
    rsi_vals = []
    for i in range(period, len(closes)):
        gains  = [max(closes[j]-closes[j-1], 0) for j in range(i-period+1, i+1)]
        losses = [max(closes[j-1]-closes[j], 0) for j in range(i-period+1, i+1)]
        ag = sum(gains)/period; al = sum(losses)/period
        rsi_vals.append(100.0 if al==0 else 100-100/(1+ag/al))
    if len(rsi_vals) < period: return 50.0
    recent = rsi_vals[-period:]
    mn, mx = min(recent), max(recent)
    if mx == mn: return 50.0
    return (rsi_vals[-1]-mn)/(mx-mn)*100

def calc_volatility(closes, period=20):
    """Annualised hourly volatility"""
    if len(closes) < period+1: return 0.5
    rets = [(closes[i]-closes[i-1])/closes[i-1] for i in range(1, len(closes))]
    recent = rets[-period:]
    mean = sum(recent)/len(recent)
    var  = sum((r-mean)**2 for r in recent)/len(recent)
    return math.sqrt(var) * math.sqrt(24*365)  # annualise

# ── v4-1: REGIME ROUTER ───────────────────────────────────────────────────────

def detect_regime(closes, volumes=None):
    """
    Returns: STRONG_TREND | TRENDING | RANGING | VOLATILE | NEUTRAL
    This is the core of every world-class algo — get the regime right,
    then apply the right strategy. Never fight the regime.
    """
    if len(closes) < 50: return "NEUTRAL"

    adx  = calc_adx(closes)
    atr  = calc_atr(closes)
    vol  = calc_volatility(closes)
    e20  = calc_ema(closes, 20)
    e50  = calc_ema(closes, 50)
    cur  = closes[-1]

    # Volatility spike = risk-off, no new entries
    # (top quant funds go flat during vol spikes — preserves prop firm DD limit)
    if vol > 3.0:  # >300% annualised vol = extreme (crypto norm is 60-120%)
        return "VOLATILE"

    # Strong directional trend
    if adx > 35 and cur > e20 > e50:
        return "STRONG_TREND_UP"
    if adx > 35 and cur < e20 < e50:
        return "STRONG_TREND_DOWN"

    # Moderate trend
    if adx > 25 and cur > e20:
        return "TRENDING_UP"
    if adx > 25 and cur < e20:
        return "TRENDING_DOWN"

    # Ranging / mean-reverting market
    if adx < 22:
        return "RANGING"

    return "NEUTRAL"

# ── v4-5: BTC MACRO FILTER ────────────────────────────────────────────────────

def get_btc_regime(btc_closes):
    """
    BTC leads the entire crypto market.
    If BTC is in downtrend, cut alt position sizes in half.
    This is a free alpha signal that top crypto funds use.
    """
    if len(btc_closes) < 50: return "NEUTRAL"
    e20 = calc_ema(btc_closes, 20)
    e50 = calc_ema(btc_closes, 50)
    cur = btc_closes[-1]
    adx = calc_adx(btc_closes)

    if cur > e20 > e50 and adx > 20: return "BULL"
    if cur < e20 < e50 and adx > 20: return "BEAR"
    return "NEUTRAL"

# ── STRATEGY SIGNALS ──────────────────────────────────────────────────────────

def trend_signal(closes):
    """Pure trend-following. Only called when regime = TRENDING/STRONG_TREND."""
    if len(closes) < 80: return "HOLD", 0
    e20  = calc_ema(closes, 20)
    e50  = calc_ema(closes, 50)
    e100 = calc_ema(closes, min(100, len(closes)))
    cur  = closes[-1]
    slope20 = (closes[-1]-closes[-20])/closes[-20]
    adx  = calc_adx(closes)

    # 4H confirmation
    c4h = closes[::4]
    e20_4h = calc_ema(c4h, min(20, len(c4h)))
    e50_4h = calc_ema(c4h, min(50, len(c4h)))
    bull_4h = c4h[-1] > e20_4h > e50_4h
    bear_4h = c4h[-1] < e20_4h < e50_4h

    score = 0; agrees = 0

    if cur > e20 > e50 > e100:  score += 4.0; agrees += 1
    elif cur > e20 > e50:        score += 2.5; agrees += 0.5
    elif cur < e20 < e50 < e100: score -= 4.0; agrees += 1
    elif cur < e20 < e50:        score -= 2.5; agrees += 0.5

    if slope20 > 0.05:    score += 2.0; agrees += 1
    elif slope20 > 0.02:  score += 1.0; agrees += 0.5
    elif slope20 < -0.05: score -= 2.0; agrees += 1
    elif slope20 < -0.02: score -= 1.0; agrees += 0.5

    if bull_4h: score += 1.5; agrees += 1
    elif bear_4h: score -= 1.5; agrees += 1

    if adx > 40: score *= 1.3
    elif adx > 35: score *= 1.15

    if score >= 3.5 and agrees >= 2 and bull_4h: return "BUY",  score
    if score <= -3.0 and agrees >= 2 and bear_4h: return "SELL", score
    return "HOLD", score

def mean_rev_signal(closes, volumes=None):
    """Pure mean reversion. Only called when regime = RANGING/NEUTRAL."""
    if len(closes) < 30: return "HOLD", 0

    upper, mid, lower = calc_bb(closes)
    rsi   = calc_rsi(closes)
    z     = calc_zscore(closes)
    stoch = calc_stoch_rsi(closes)
    cur   = closes[-1]

    # Volume confirmation
    if volumes and len(volumes) >= 20:
        avg_vol = sum(volumes[-20:])/20
        if volumes[-1] < avg_vol * 0.7: return "HOLD", 0

    score = 0; agrees = 0

    if cur < lower:   score += 3.0; agrees += 1
    elif cur > upper: score -= 3.0; agrees += 1

    if z < -2.0:   score += 2.5; agrees += 1
    elif z < -1.5: score += 1.2; agrees += 0.5
    elif z > 2.0:  score -= 2.5; agrees += 1
    elif z > 1.5:  score -= 1.2; agrees += 0.5

    if rsi < 25:   score += 2.0; agrees += 1
    elif rsi < 35: score += 1.0; agrees += 0.5
    elif rsi > 75: score -= 2.0; agrees += 1
    elif rsi > 65: score -= 1.0; agrees += 0.5

    if stoch < 15:  score += 1.5; agrees += 0.5
    elif stoch > 85: score -= 1.5; agrees += 0.5

    # v4-4: Simulated funding rate proxy
    # High recent momentum = crowded longs = extra mean rev buy signal on dip
    momentum_10 = (closes[-1]-closes[-10])/closes[-10] if len(closes)>=10 else 0
    if momentum_10 < -0.03 and rsi < 35: score += 1.5  # Selling panic = buy
    if momentum_10 > 0.05  and rsi > 65: score -= 1.5  # Euphoria = sell

    if score >= 3.5 and agrees >= 2: return "BUY",  score
    if score <= -3.5 and agrees >= 2: return "SELL", score
    return "HOLD", score

# ── v4-2: VOLATILITY-ADJUSTED POSITION SIZING ────────────────────────────────

def calc_position_size(equity, atr, price, risk_pct=0.01, max_pct=0.06):
    """
    Professional quant sizing: risk a fixed % of equity per trade
    Position size = (Equity × Risk%) / (ATR × stop_multiplier)
    This means small positions in volatile markets, larger in calm ones.
    Capped at max_pct of equity to prevent over-concentration.
    """
    risk_dollars = equity * risk_pct
    atr_stop     = atr * 1.5  # 1.5x ATR stop distance
    if atr_stop <= 0 or price <= 0: return 0
    qty = risk_dollars / atr_stop
    spend = qty * price
    # Cap at max_pct of equity
    max_spend = equity * max_pct
    if spend > max_spend:
        spend = max_spend
        qty   = spend / price
    return spend, qty

# ── REGIME-ADAPTIVE BACKTEST ENGINE ──────────────────────────────────────────

class RegimeAdaptiveBacktest:
    """
    v4: Single unified engine that routes each symbol to the right
    strategy based on real-time regime detection.
    This is what Two Sigma / Renaissance-style systems do at scale.
    """
    def __init__(self, name, symbols, cash=100000,
                 portfolio_dd_brake=0.06,   # v4-3: pause if 6% portfolio DD
                 portfolio_dd_resume=0.04): # resume at 4% DD
        self.name       = name
        self.symbols    = symbols
        self.cash       = cash
        self.starting   = cash
        self.positions  = {}
        self.trades     = []
        self.equity_curve = [cash]
        self.peak       = cash
        self.max_dd     = 0
        self.dd_brake_active = False
        self.portfolio_dd_brake  = portfolio_dd_brake
        self.portfolio_dd_resume = portfolio_dd_resume
        # Per-symbol regime tracking
        self.regime_log = {}
        self.daily_pnl  = 0
        self.last_day   = None

    def run(self, all_bars):
        max_len = max((len(all_bars.get(s,[])) for s in self.symbols), default=0)
        btc_bars = all_bars.get("BTC/USD", [])
        print(f"  [{self.name}] {max_len} bars, {len(self.symbols)} symbols...")

        for i in range(100, max_len):
            # ── Portfolio equity ──
            total = self.cash
            for sym, pos in self.positions.items():
                bars = all_bars.get(sym, [])
                if i < len(bars):
                    total += pos["qty"] * bars[i]["close"]
            self.equity_curve.append(total)
            if total > self.peak: self.peak = total
            dd = (self.peak - total) / self.peak * 100
            if dd > self.max_dd: self.max_dd = dd

            # v4-3: Portfolio drawdown brake
            if dd >= self.portfolio_dd_brake * 100:
                self.dd_brake_active = True
            elif dd <= self.portfolio_dd_resume * 100:
                self.dd_brake_active = False

            # Daily loss guard
            for sym in self.symbols:
                bars = all_bars.get(sym, [])
                if i < len(bars):
                    ts  = bars[i].get("timestamp", 0)
                    day = ts // 86400
                    if self.last_day != day:
                        self.daily_pnl = 0
                        self.last_day  = day
                    break
            daily_blocked = self.daily_pnl < -(self.starting * 0.02)

            # v4-5: BTC macro filter
            btc_closes = [b["close"] for b in btc_bars[:i+1]] if btc_bars else []
            btc_regime = get_btc_regime(btc_closes) if len(btc_closes) >= 50 else "NEUTRAL"
            btc_size_mult = 0.5 if btc_regime == "BEAR" else 1.0

            for sym in self.symbols:
                bars = all_bars.get(sym, [])
                if i >= len(bars): continue
                closes  = [b["close"] for b in bars[:i+1]]
                volumes = [b["volume"] for b in bars[:i+1]]
                cur     = closes[-1]
                atr     = calc_atr(closes)
                held    = sym in self.positions

                # ── v4-1: Detect regime for this symbol ──
                regime = detect_regime(closes, volumes)
                self.regime_log[sym] = regime

                # ── Manage open position ──
                if held:
                    pos    = self.positions[sym]
                    entry  = pos["entry"]
                    strat  = pos["strategy"]
                    profit = (cur - entry) * pos["qty"]

                    # v4-6: Dynamic TP based on ADX at entry
                    adx_at_entry = pos.get("adx", 25)
                    if adx_at_entry > 40:
                        take_mult = 99.0  # trailing only
                    elif adx_at_entry > 30:
                        take_mult = 4.0
                    else:
                        take_mult = 2.5

                    # Trailing stop for strong trend positions
                    if strat == "TREND" and profit > 0 and cur > entry + atr*1.5:
                        if cur > pos.get("trail_high", cur):
                            pos["trail_high"] = cur
                        trail_high = pos.get("trail_high", cur)
                        if cur < trail_high * 0.98:  # 2% trail
                            self._close(sym, pos, cur, i, "Trailing stop")
                            continue

                    # Stop loss (all strategies)
                    if cur < entry - atr * 1.5:
                        self._close(sym, pos, cur, i, "Stop loss")
                        continue

                    # Fixed take profit (mean rev / neutral)
                    if strat != "TREND" and cur > entry + atr * take_mult:
                        self._close(sym, pos, cur, i, "Take profit")
                        continue

                    # Exit mean rev if regime flips to strong trend (don't fight trend)
                    if strat == "MEAN_REV" and "STRONG_TREND" in regime:
                        self._close(sym, pos, cur, i, "Regime exit")
                        continue

                # ── Entry logic ──
                if self.dd_brake_active or daily_blocked:
                    continue
                if len(self.positions) >= 5:
                    continue
                if held:
                    continue

                # Route to correct strategy based on regime
                sig = "HOLD"; score = 0; strat_used = None

                if regime in ("STRONG_TREND_UP", "STRONG_TREND_DOWN", "TRENDING_UP", "TRENDING_DOWN"):
                    sig, score = trend_signal(closes)
                    strat_used = "TREND"
                elif regime in ("RANGING", "NEUTRAL"):
                    sig, score = mean_rev_signal(closes, volumes)
                    strat_used = "MEAN_REV"
                # VOLATILE = no signal at all

                if sig == "BUY" and self.cash > 100:
                    result = calc_position_size(total, atr, cur)
                    if result:
                        spend, qty = result
                        spend *= btc_size_mult  # v4-5: halve in BTC bear
                        qty   *= btc_size_mult
                        if spend > 20 and spend <= self.cash:
                            self.cash -= spend
                            self.positions[sym] = {
                                "qty": qty, "entry": cur, "entry_idx": i,
                                "value": spend, "score": score,
                                "strategy": strat_used,
                                "trail_high": cur,
                                "adx": calc_adx(closes),
                            }
                elif sig == "SELL" and held:
                    self._close(sym, self.positions[sym], cur, i, "Signal")

        # Close all remaining
        for sym, pos in list(self.positions.items()):
            bars = all_bars.get(sym, [])
            if not bars: continue
            cur = bars[-1]["close"]
            self._close(sym, pos, cur, max_len, "End of period")

    def _close(self, sym, pos, cur, bar_idx, reason):
        profit = (cur - pos["entry"]) * pos["qty"]
        self.cash += cur * pos["qty"]
        self.daily_pnl += profit
        self.trades.append({
            "symbol": sym, "entry": pos["entry"], "exit": cur,
            "qty": pos["qty"], "profit": profit,
            "profit_pct": profit/pos["value"]*100 if pos["value"] else 0,
            "reason": reason, "bars_held": bar_idx - pos["entry_idx"],
            "strategy": pos.get("strategy","?"),
        })
        if sym in self.positions:
            del self.positions[sym]

    def results(self):
        final = self.cash
        ret   = (final - self.starting) / self.starting * 100
        wins    = [t for t in self.trades if t["profit"] > 0]
        losses  = [t for t in self.trades if t["profit"] <= 0]
        wr  = len(wins)/len(self.trades)*100 if self.trades else 0
        avg_win  = sum(t["profit"] for t in wins)/len(wins)     if wins   else 0
        avg_loss = abs(sum(t["profit"] for t in losses)/len(losses)) if losses else 0
        pf = (sum(t["profit"] for t in wins) /
              abs(sum(t["profit"] for t in losses))
              if losses and wins else 0)
        if len(self.equity_curve) > 1:
            rets  = [(self.equity_curve[i]-self.equity_curve[i-1])/self.equity_curve[i-1]
                     for i in range(1, len(self.equity_curve))]
            avg_r = sum(rets)/len(rets)
            std_r = math.sqrt(sum((r-avg_r)**2 for r in rets)/len(rets)) if rets else 1
            sharpe = avg_r/std_r*math.sqrt(24*365) if std_r > 0 else 0
        else:
            sharpe = 0
        by_sym = {}
        for t in self.trades:
            by_sym[t["symbol"]] = by_sym.get(t["symbol"],0) + t["profit"]
        best  = max(by_sym, key=by_sym.get) if by_sym else "N/A"
        worst = min(by_sym, key=by_sym.get) if by_sym else "N/A"

        # Strategy breakdown
        trend_trades = [t for t in self.trades if t.get("strategy")=="TREND"]
        rev_trades   = [t for t in self.trades if t.get("strategy")=="MEAN_REV"]

        avg_hold = sum(t["bars_held"] for t in self.trades)/len(self.trades) if self.trades else 0
        return {
            "strategy": self.name, "starting": self.starting, "final": round(final,2),
            "return_pct": round(ret,2), "profit": round(final-self.starting,2),
            "trades": len(self.trades), "wins": len(wins), "losses": len(losses),
            "win_rate": round(wr,1), "avg_win": round(avg_win,2), "avg_loss": round(avg_loss,2),
            "profit_factor": round(pf,2), "max_dd": round(self.max_dd,2),
            "sharpe": round(sharpe,2), "avg_hold_hrs": round(avg_hold,1),
            "best_symbol": best,  "best_pnl":  round(by_sym.get(best,0),2),
            "worst_symbol": worst,"worst_pnl": round(by_sym.get(worst,0),2),
            "trend_trades": len(trend_trades),
            "mean_rev_trades": len(rev_trades),
            "equity_curve": [round(e,2) for e in self.equity_curve[::20]],
        }

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_backtest():
    print("\n" + "="*60)
    print("APEX BACKTEST v4 — REGIME-ADAPTIVE ENGINE")
    print("Strategy: Detect regime first, apply right strategy")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*60)

    # All symbols — more = more regime-appropriate opportunities
    bot1_syms = ["BTC/USD","ETH/USD","SOL/USD","AVAX/USD","LINK/USD"]
    bot2_syms = ["ETH/USD","BCH/USD","LTC/USD","AAVE/USD","BTC/USD","AVAX/USD"]
    bot3_syms = ["BTC/USD","ETH/USD","SOL/USD","AVAX/USD","LINK/USD","LTC/USD"]
    all_syms  = list(set(bot1_syms + bot2_syms + bot3_syms))

    print(f"\n[1/4] Downloading 6 months of real data...")
    all_bars = {}
    for sym in all_syms:
        bars = fetch_historical(sym, months=6)
        if bars: all_bars[sym] = bars
    total_bars = sum(len(v) for v in all_bars.values())
    print(f"  {len(all_bars)} symbols, {total_bars:,} bars total")

    print(f"\n[2/4] Running v4 regime-adaptive backtests...")

    bt1 = RegimeAdaptiveBacktest("REGIME_BOT_1", bot1_syms, cash=100000)
    bt1.run({k: all_bars.get(k,[]) for k in bot1_syms})
    r1 = bt1.results()

    bt2 = RegimeAdaptiveBacktest("REGIME_BOT_2", bot2_syms, cash=100000)
    bt2.run({k: all_bars.get(k,[]) for k in bot2_syms})
    r2 = bt2.results()

    bt3 = RegimeAdaptiveBacktest("REGIME_BOT_3", bot3_syms, cash=100000)
    bt3.run({k: all_bars.get(k,[]) for k in bot3_syms})
    r3 = bt3.results()

    results      = [r1, r2, r3]
    total_start  = 300000
    total_final  = r1["final"] + r2["final"] + r3["final"]
    total_ret    = (total_final - total_start) / total_start * 100
    total_trades = r1["trades"] + r2["trades"] + r3["trades"]
    total_wins   = r1["wins"]   + r2["wins"]   + r3["wins"]
    combined_wr  = total_wins / total_trades * 100 if total_trades else 0
    worst_dd     = max(r["max_dd"] for r in results)
    avg_pf       = sum(r["profit_factor"] for r in results) / 3
    avg_sharpe   = sum(r["sharpe"] for r in results) / 3
    prop_ready   = total_ret >= 10 and worst_dd < 10

    total_trend  = sum(r["trend_trades"]    for r in results)
    total_rev    = sum(r["mean_rev_trades"] for r in results)

    summary = {
        "type": "backtest_v4", "version": "regime_adaptive",
        "approach": "Detect regime first, route to trend or mean_rev automatically",
        "upgrades": [
            "regime_router_per_symbol_per_bar",
            "volatility_adjusted_position_sizing",
            "portfolio_drawdown_brake_6pct",
            "funding_rate_proxy_signal",
            "btc_macro_filter_alts",
            "dynamic_take_profit_by_adx",
        ],
        "period": "6 months",
        "run_date": datetime.now(timezone.utc).isoformat(),
        "total_start": total_start,
        "total_final": round(total_final, 2),
        "total_return_pct": round(total_ret, 2),
        "total_profit": round(total_final - total_start, 2),
        "total_trades": total_trades,
        "trend_trades": total_trend,
        "mean_rev_trades": total_rev,
        "combined_win_rate": round(combined_wr, 1),
        "worst_drawdown": round(worst_dd, 2),
        "avg_profit_factor": round(avg_pf, 2),
        "avg_sharpe": round(avg_sharpe, 2),
        "prop_firm_ready": prop_ready,
        "strategies": results,
    }

    print(f"\n[3/4] RESULTS — v4 REGIME-ADAPTIVE")
    print(f"\n{'BOT':<16}{'RETURN':>9}{'WIN%':>7}{'TRADES':>8}{'DD%':>7}{'SHARPE':>8}{'PF':>6}")
    print("-"*62)
    for r in results:
        print(f"{r['strategy']:<16}{r['return_pct']:>8.1f}%{r['win_rate']:>6.1f}%"
              f"{r['trades']:>8}{r['max_dd']:>6.1f}%{r['sharpe']:>8.2f}{r['profit_factor']:>6.2f}")
    print(f"\n{'COMBINED':<16}{total_ret:>8.1f}%{combined_wr:>6.1f}%{total_trades:>8}{worst_dd:>6.1f}%")
    print(f"\n$300K → ${total_final:,.0f} | Profit: ${total_final-total_start:+,.0f}")
    print(f"Avg Profit Factor: {avg_pf:.2f} | Avg Sharpe: {avg_sharpe:.2f}")
    print(f"Trade split: {total_trend} trend trades / {total_rev} mean-rev trades")
    print(f"\nProp Firm Ready: {'✓ YES — Apply Now!' if prop_ready else 'NOT YET'}")

    if not prop_ready:
        print(f"\nGap to target:")
        if total_ret < 10:
            print(f"  Return:   {total_ret:.1f}% (need 10.0%, gap = {10-total_ret:.1f}%)")
        if worst_dd >= 10:
            print(f"  Drawdown: {worst_dd:.1f}% (need < 10%)")

    print(f"\nPer Bot:")
    for r in results:
        print(f"  {r['strategy']}: {r['return_pct']:+.1f}% | "
              f"WR:{r['win_rate']:.0f}% | "
              f"Holds avg {r['avg_hold_hrs']:.0f}hrs | "
              f"Best:{r['best_symbol']} ${r['best_pnl']:+,.0f} | "
              f"Trend:{r['trend_trades']} MeanRev:{r['mean_rev_trades']}")

    print(f"\n[4/4] Saving...")
    try:
        sb_url = os.environ.get("SUPABASE_URL")
        sb_key = os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text = (
                f"BACKTEST v4 — REGIME-ADAPTIVE ENGINE — 6 Month Test\n\n"
                f"APPROACH: World-class quant method\n"
                f"  Detect market regime per symbol per bar\n"
                f"  Route to TREND strategy in trending markets\n"
                f"  Route to MEAN_REV strategy in ranging markets\n"
                f"  Go flat in volatile/extreme conditions\n\n"
                f"UPGRADES FROM v3:\n"
                f"  Regime router (replaces 3 fixed strategies)\n"
                f"  Volatility-adjusted position sizing (ATR-based Kelly)\n"
                f"  Portfolio DD brake at 6% (prop firm safe zone)\n"
                f"  BTC macro filter (50% size in BTC bear market)\n"
                f"  Dynamic TP by ADX (2.5x weak / 4x strong / trailing v.strong)\n\n"
                f"RESULTS:\n"
                f"Total Return:    {total_ret:+.1f}%\n"
                f"Win Rate:        {combined_wr:.0f}%\n"
                f"Max Drawdown:    {worst_dd:.1f}%\n"
                f"Total Trades:    {total_trades} ({total_trend} trend / {total_rev} mean-rev)\n"
                f"Avg PF:          {avg_pf:.2f}\n"
                f"Avg Sharpe:      {avg_sharpe:.2f}\n"
                f"Prop Ready:      {'YES' if prop_ready else 'NOT YET'}\n\n"
                f"BOT 1: {r1['return_pct']:+.1f}% | WR:{r1['win_rate']:.0f}% | {r1['trades']} trades | DD:{r1['max_dd']:.1f}%\n"
                f"BOT 2: {r2['return_pct']:+.1f}% | WR:{r2['win_rate']:.0f}% | {r2['trades']} trades | DD:{r2['max_dd']:.1f}%\n"
                f"BOT 3: {r3['return_pct']:+.1f}% | WR:{r3['win_rate']:.0f}% | {r3['trades']} trades | DD:{r3['max_dd']:.1f}%"
            )
            payload = json.dumps({
                "week_ending":  datetime.now(timezone.utc).isoformat(),
                "report_text":  report_text,
                "bot_data":     summary,
                "news_context": json.dumps({"type": "backtest_v4"}),
            }).encode()
            req = urllib.request.Request(
                f"{sb_url}/rest/v1/reports", data=payload,
                headers={
                    "Content-Type":  "application/json",
                    "apikey":        sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Prefer":        "return=minimal",
                },
                method="POST"
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
