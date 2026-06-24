"""
APEX BACKTEST v8c — EUR/USD MEAN REVERSION (FRESH 6-MONTH RUN)
================================================================
Re-run of the locked v8c strategy on the most recent 6 months of data
(whatever "today" is when this script executes).

LOCKED PARAMS (proven across 3 historical windows):
  bb_period=10, bb_std=2.0, rsi_period=7, rsi_ob=65, rsi_os=40
  atr_stop=2.0, atr_tp=1.5, min_score=3
  risk_per_trade=0.4%, max_total_loss=6% (internal safety buffer)

WALK-FORWARD METHODOLOGY:
  In-sample:     first 4 months of the fetched window
  Out-of-sample: last 2 months of the fetched window (never touched
                 during parameter selection — params are already locked
                 from prior testing, so this run is itself a fresh
                 out-of-sample test on entirely new data)

FTMO-STYLE TARGETS (for reference):
  Return:    >= 10%    (8% on some firms in 2026 — checked against both)
  Drawdown:  < 10%
  Daily DD:  < 5%
"""

import json, math, urllib.request, os
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
                "close":  c,
                "high":   ohlcv["high"][i] or c,
                "low":    ohlcv["low"][i]  or c,
                "volume": ohlcv["volume"][i] or 0,
                "hour_est": ((ts % 86400) // 3600 - 5) % 24,
            })
        session = [b for b in bars if 7 <= b["hour_est"] <= 17]
        print(f"  EUR/USD: {len(bars)} total bars, "
              f"{len(session)} session bars (07-17 EST)")
        if bars:
            start_date = datetime.fromtimestamp(bars[0]["timestamp"], tz=timezone.utc)
            end_date   = datetime.fromtimestamp(bars[-1]["timestamp"], tz=timezone.utc)
            print(f"  Date range: {start_date.strftime('%Y-%m-%d')} "
                  f"to {end_date.strftime('%Y-%m-%d')}")
        return session
    except Exception as e:
        print(f"  EUR/USD fetch failed: {e}")
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
    if al==0: return 100.0
    return 100-100/(1+ag/al)

def calc_atr(closes, period=14):
    if len(closes)<2: return abs(closes[-1])*0.001
    trs=[abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
    return sum(trs[-period:])/min(len(trs),period)

def calc_zscore(closes, period=20):
    if len(closes)<period: return 0.0
    sl=closes[-period:]; mid=sum(sl)/period
    std=math.sqrt(sum((x-mid)**2 for x in sl)/period) or 1e-10
    return (closes[-1]-mid)/std

# ── BACKTEST (locked v8c logic) ───────────────────────────────────────────────

def run_backtest(bars, start_idx, end_idx, starting_cash=50000):
    bb_period=10; bb_std=2.0; rsi_period=7
    rsi_ob=65; rsi_os=40; atr_stop=2.0; atr_tp=1.5
    min_score=3; risk_per_trade=0.004; max_total_loss=0.06
    max_daily_loss=0.035

    cash=starting_cash; position=None; trades=[]
    equity_curve=[cash]; peak=cash; max_dd=0
    trading_days=set(); daily_pnl={}; killed=False
    warmup=max(bb_period,rsi_period,20)+5

    for i in range(warmup, end_idx):
        if i < start_idx:
            equity_curve.append(cash); continue
        if killed:
            equity_curve.append(cash); continue

        bar=bars[i]; cur=bar["close"]
        ts=bar["timestamp"]; day=ts//86400

        equity=cash
        if position:
            po=(cur-position["entry"])*position["size"]
            if position["side"]=="SELL": po=-po
            equity=cash+po
        equity_curve.append(equity)
        if equity>peak: peak=equity
        dd=(peak-equity)/peak*100
        if dd>max_dd: max_dd=dd

        if (starting_cash-equity)/starting_cash >= max_total_loss:
            if position:
                pnl=(cur-position["entry"])*position["size"]
                if position["side"]=="SELL": pnl=-pnl
                cash+=pnl
                trades.append({**position,"exit":cur,"pnl":pnl,
                    "exit_reason":"DD limit","bars_held":i-position["bar_idx"]})
                position=None
            killed=True; continue

        day_pnl=daily_pnl.get(day,0)
        if day_pnl<=-(starting_cash*max_daily_loss):
            if position:
                pnl=(cur-position["entry"])*position["size"]
                if position["side"]=="SELL": pnl=-pnl
                cash+=pnl
                daily_pnl[day]=daily_pnl.get(day,0)+pnl
                trades.append({**position,"exit":cur,"pnl":pnl,
                    "exit_reason":"Daily limit","bars_held":i-position["bar_idx"]})
                position=None
            continue

        closes=[b["close"] for b in bars[max(0,i-200):i+1]]
        atr=calc_atr(closes)

        if position:
            pnl=(cur-position["entry"])*position["size"]
            if position["side"]=="SELL": pnl=-pnl
            hs=((position["side"]=="BUY"  and cur<=position["stop"]) or
                (position["side"]=="SELL" and cur>=position["stop"]))
            ht=((position["side"]=="BUY"  and cur>=position["tp"]) or
                (position["side"]=="SELL" and cur<=position["tp"]))
            if hs or ht:
                cash+=pnl
                daily_pnl[day]=daily_pnl.get(day,0)+pnl
                trades.append({**position,"exit":cur,"pnl":pnl,
                    "exit_reason":"TP" if ht else "Stop",
                    "bars_held":i-position["bar_idx"]})
                trading_days.add(day); position=None
            continue

        upper,mid,lower=calc_bb(closes,bb_period,bb_std)
        rsi=calc_rsi(closes,rsi_period)
        z=calc_zscore(closes,bb_period)
        prev=closes[-2] if len(closes)>=2 else cur

        bs=0
        if cur<lower:  bs+=1
        if rsi<rsi_os: bs+=1
        if z<-1.0:     bs+=1
        if cur>prev:   bs+=1

        ss=0
        if cur>upper:  ss+=1
        if rsi>rsi_ob: ss+=1
        if z>1.0:      ss+=1
        if cur<prev:   ss+=1

        size=(cash*risk_per_trade)/(atr*atr_stop) if atr*atr_stop>0 else 0
        if size<=0: continue

        if bs>=min_score and not position:
            position={"side":"BUY","entry":cur,
                "stop":cur-atr*atr_stop,"tp":cur+atr*atr_tp,
                "size":size,"bar_idx":i,"entry_ts":ts}
        elif ss>=min_score and not position:
            position={"side":"SELL","entry":cur,
                "stop":cur+atr*atr_stop,"tp":cur-atr*atr_tp,
                "size":size,"bar_idx":i,"entry_ts":ts}

    if position and bars:
        cur=bars[min(end_idx-1,len(bars)-1)]["close"]
        pnl=(cur-position["entry"])*position["size"]
        if position["side"]=="SELL": pnl=-pnl
        cash+=pnl
        trades.append({**position,"exit":cur,"pnl":pnl,
            "exit_reason":"End","bars_held":end_idx-position["bar_idx"]})

    final=cash; ret=(final-starting_cash)/starting_cash*100
    wins=[t for t in trades if t["pnl"]>0]
    losses=[t for t in trades if t["pnl"]<=0]
    wr=len(wins)/len(trades)*100 if trades else 0
    pf=(sum(t["pnl"] for t in wins)/abs(sum(t["pnl"] for t in losses))
        if wins and losses else 0)

    sharpe=0
    if len(equity_curve)>1:
        rets=[(equity_curve[j]-equity_curve[j-1])/equity_curve[j-1]
              for j in range(1,len(equity_curve)) if equity_curve[j-1]>0]
        if rets:
            avg_r=sum(rets)/len(rets)
            std_r=math.sqrt(sum((r-avg_r)**2 for r in rets)/len(rets))
            sharpe=avg_r/std_r*math.sqrt(24*252) if std_r>0 else 0

    daily_returns={}
    for t in trades:
        d=t["entry_ts"]//86400
        daily_returns[d]=daily_returns.get(d,0)+t["pnl"]
    worst_day=min((v/starting_cash*100 for v in daily_returns.values()),default=0)

    return {
        "return_pct":    round(ret,3),
        "final":         round(final,2),
        "profit":        round(final-starting_cash,2),
        "trades":        len(trades),
        "win_rate":      round(wr,1),
        "profit_factor": round(pf,3),
        "max_dd":        round(max_dd,3),
        "sharpe":        round(sharpe,3),
        "worst_day_pct": round(worst_day,3),
        "trading_days":  len(trading_days),
        "killed":        killed,
        "ftmo_pass_10":  (ret>=10.0 and max_dd<10.0 and worst_day>-5.0),
        "ftmo_pass_8":   (ret>=8.0  and max_dd<10.0 and worst_day>-5.0),
        "equity_curve":  [round(e,2) for e in equity_curve[::10]],
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_full_backtest():
    print("\n"+"="*65)
    print("APEX BACKTEST v8c — FRESH 6-MONTH RUN")
    print("Locked params | Most recent available data")
    print(f"Run date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*65)

    print(f"\n[1/4] Downloading EUR/USD 6-month hourly data...")
    session_bars = fetch_eurusd(months=6)

    if len(session_bars) < 200:
        print("Insufficient data. Exiting.")
        return {}

    total     = len(session_bars)
    is_end    = int(total * (4/6))
    oos_start = is_end
    oos_end   = total

    print(f"\n  Session bars total: {total}")
    print(f"  In-sample:     0–{is_end} ({is_end} bars ≈ 4 months)")
    print(f"  Out-of-sample: {oos_start}–{oos_end} "
          f"({oos_end-oos_start} bars ≈ 2 months)")
    print(f"  This entire window is NEW data not used when params were locked")

    print(f"\n[2/4] In-sample check (4mo)...")
    ins = run_backtest(session_bars, 0, is_end)
    print(f"  Return: {ins['return_pct']:+.2f}% | "
          f"Sharpe: {ins['sharpe']:.3f} | "
          f"DD: {ins['max_dd']:.2f}% | "
          f"Trades: {ins['trades']}")

    print(f"\n[3/4] Out-of-sample check (2mo, most recent)...")
    oos = run_backtest(session_bars, oos_start, oos_end)
    print(f"  Return: {oos['return_pct']:+.2f}% | "
          f"Sharpe: {oos['sharpe']:.3f} | "
          f"DD: {oos['max_dd']:.2f}% | "
          f"Trades: {oos['trades']}")

    print(f"\n[4/4] Full 6-month simulation ($50K)...")
    full = run_backtest(session_bars, 0, total, starting_cash=50000)

    print(f"\n{'='*65}")
    print(f"RESULTS — FRESH 6-MONTH RUN")
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
    print(f"{'Full 6mo ($50K)':<24}"
          f"{full['return_pct']:>7.2f}%{full['win_rate']:>6.1f}%"
          f"{full['trades']:>8}{full['max_dd']:>6.2f}%"
          f"{full['sharpe']:>9.3f}{full['profit_factor']:>7.3f}")

    print(f"\n$50K → ${full['final']:,.2f} | "
          f"Profit: ${full['profit']:+,.2f}")

    print(f"\nPROP FIRM RULE COMPLIANCE (full 6mo sim):")
    print(f"  Daily loss:    {'PASS' if full['worst_day_pct']>-5.0 else 'FAIL'} "
          f"(worst day: {full['worst_day_pct']:.2f}% vs -5% limit)")
    print(f"  Max drawdown:  {'PASS' if full['max_dd']<10.0 else 'FAIL'} "
          f"({full['max_dd']:.2f}% vs 10% limit)")
    print(f"  Target (10%):  {'PASS' if full['ftmo_pass_10'] else 'NOT YET'} "
          f"({full['return_pct']:.2f}% vs 10% target)")
    print(f"  Target (8%):   {'PASS' if full['ftmo_pass_8'] else 'NOT YET'} "
          f"({full['return_pct']:.2f}% vs 8% target)")
    print(f"  Trading days:  {full['trading_days']}")

    is_ret  = ins["return_pct"]
    oos_ret = oos["return_pct"]
    decay   = is_ret - oos_ret

    print(f"\n{'='*65}")
    print(f"HONEST VERDICT")
    print(f"{'='*65}")
    print(f"\nIn-sample:     {is_ret:+.2f}%")
    print(f"Out-of-sample: {oos_ret:+.2f}%")
    print(f"Decay:         {decay:+.2f}% (< 3% good, > 5% concerning)")

    if oos["sharpe"] > 0.5 and oos_ret > 0:
        verdict = "EDGE HOLDS — positive OOS on fresh data, good Sharpe"
    elif oos_ret > 0:
        verdict = "WEAK EDGE — OOS positive but Sharpe low, watch closely"
    elif abs(decay) < 3 and oos_ret > -2:
        verdict = "INCONCLUSIVE — small decay, modestly unprofitable OOS"
    else:
        verdict = "EDGE WEAKENED — meaningful decay on this fresh window"

    print(f"\nVerdict: {verdict}")

    if full['ftmo_pass_8'] or full['ftmo_pass_10']:
        print(f"\nThis run would PASS a prop firm evaluation.")
    else:
        gap = 8.0 - full['return_pct'] if full['return_pct'] < 8 else 0
        print(f"\nThis run would NOT yet pass. Gap to 8% target: {gap:.2f}%")

    summary = {
        "type": "backtest_v8c_fresh_run",
        "run_date": datetime.now(timezone.utc).isoformat(),
        "in_sample":     {k:v for k,v in ins.items()  if k!="equity_curve"},
        "out_of_sample": {k:v for k,v in oos.items()  if k!="equity_curve"},
        "full_6mo":      {k:v for k,v in full.items() if k!="equity_curve"},
        "verdict": verdict,
    }

    try:
        sb_url=os.environ.get("SUPABASE_URL")
        sb_key=os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text=(
                f"BACKTEST v8c — FRESH 6-MONTH RUN\n"
                f"Run date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
                f"In-sample:     {is_ret:+.2f}% | DD:{ins['max_dd']:.2f}%\n"
                f"Out-of-sample: {oos_ret:+.2f}% | DD:{oos['max_dd']:.2f}%\n"
                f"Full 6mo:      {full['return_pct']:+.2f}% | DD:{full['max_dd']:.2f}%\n"
                f"Verdict: {verdict}\n"
                f"Pass 8% target: {full['ftmo_pass_8']}\n"
                f"Pass 10% target: {full['ftmo_pass_10']}"
            )
            payload=json.dumps({
                "week_ending": datetime.now(timezone.utc).isoformat(),
                "report_text": report_text,
                "bot_data": summary,
                "news_context": json.dumps({"type":"backtest_v8c_fresh"}),
            }).encode()
            req=urllib.request.Request(
                f"{sb_url}/rest/v1/reports",data=payload,
                headers={"Content-Type":"application/json",
                         "apikey":sb_key,
                         "Authorization":f"Bearer {sb_key}",
                         "Prefer":"return=minimal"},
                method="POST")
            with urllib.request.urlopen(req,timeout=15):
                print(f"\nSaved to Supabase!")
    except Exception as e:
        print(f"\nSupabase save failed: {e}")

    with open("backtest_results.json","w") as f:
        json.dump(summary,f,indent=2,default=str)
    print(f"Saved backtest_results.json")
    return summary

if __name__=="__main__":
    run_full_backtest()
