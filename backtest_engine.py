"""
APEX BACKTEST v8d — MULTI-WINDOW VALIDATION
============================================
PURPOSE: Test the v8c strategy across MULTIPLE time windows to confirm
the edge is real and not specific to one 6-month period.

WINDOWS TESTED:
  Window 1: Last 6 months  (most recent)
  Window 2: Last 12 months (longer history)
  Window 3: Months 7-12 ago (older window, completely different period)

LOCKED PARAMS FROM v8c (no changes):
  bb_period=10, bb_std=2.0, rsi_period=7, rsi_ob=65, rsi_os=40
  atr_stop=2.0, atr_tp=1.5, min_score=3
  risk_per_trade=0.4%, max_total_loss=6%

If the strategy is profitable across ALL THREE windows, the edge is real.
If it only works on one window, it was luck.
"""

import json, math, urllib.request, os
from datetime import datetime, timezone

def fetch_eurusd(months=12):
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
              f"{len(session)} session bars")
        return session
    except Exception as e:
        print(f"  EUR/USD fetch failed: {e}")
        return []

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
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    if al==0: return 100.0
    return 100-100/(1+ag/al)

def calc_atr(closes, period=14):
    if len(closes)<2: return abs(closes[-1])*0.001
    trs = [abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
    return sum(trs[-period:])/min(len(trs),period)

def calc_zscore(closes, period=20):
    if len(closes)<period: return 0.0
    sl=closes[-period:]; mid=sum(sl)/period
    std=math.sqrt(sum((x-mid)**2 for x in sl)/period) or 1e-10
    return (closes[-1]-mid)/std

def run_backtest(bars, start_idx, end_idx, starting_cash=50000):
    # LOCKED params from v8c
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
        "ftmo_pass":     (ret>=8.0 and max_dd<10.0 and worst_day>-5.0),
        "equity_curve":  [round(e,2) for e in equity_curve[::10]],
    }

def run_full_backtest():
    print("\n"+"="*65)
    print("APEX BACKTEST v8d — MULTI-WINDOW VALIDATION")
    print("Testing v8c strategy across 3 different time windows")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*65)

    print(f"\n[1/3] Downloading 12 months of EUR/USD data...")
    bars_12mo = fetch_eurusd(months=12)
    if not bars_12mo:
        print("No data. Exiting."); return {}

    total = len(bars_12mo)
    half  = total // 2

    # Window 1: Last 6 months (same as v8c — should match)
    w1_start = half
    w1_end   = total

    # Window 2: Full 12 months
    w2_start = 0
    w2_end   = total

    # Window 3: First 6 months (completely different period)
    w3_start = 0
    w3_end   = half

    print(f"  Total bars: {total}")
    print(f"  Window 1 (recent 6mo):  bars {w1_start}–{w1_end}")
    print(f"  Window 2 (full 12mo):   bars {w2_start}–{w2_end}")
    print(f"  Window 3 (older 6mo):   bars {w3_start}–{w3_end}")

    print(f"\n[2/3] Running all windows...")
    r1 = run_backtest(bars_12mo, w1_start, w1_end)
    print(f"  Window 1 (recent 6mo):  "
          f"Return={r1['return_pct']:+.2f}% | "
          f"Sharpe={r1['sharpe']:.3f} | "
          f"DD={r1['max_dd']:.2f}% | "
          f"Trades={r1['trades']}")

    r2 = run_backtest(bars_12mo, w2_start, w2_end)
    print(f"  Window 2 (full 12mo):   "
          f"Return={r2['return_pct']:+.2f}% | "
          f"Sharpe={r2['sharpe']:.3f} | "
          f"DD={r2['max_dd']:.2f}% | "
          f"Trades={r2['trades']}")

    r3 = run_backtest(bars_12mo, w3_start, w3_end)
    print(f"  Window 3 (older 6mo):   "
          f"Return={r3['return_pct']:+.2f}% | "
          f"Sharpe={r3['sharpe']:.3f} | "
          f"DD={r3['max_dd']:.2f}% | "
          f"Trades={r3['trades']}")

    print(f"\n[3/3] Results")
    print(f"\n{'='*65}")
    print(f"MULTI-WINDOW VALIDATION RESULTS")
    print(f"{'='*65}")
    print(f"\n{'WINDOW':<24}{'RET%':>7}{'WR%':>6}{'TRADES':>8}"
          f"{'DD%':>7}{'SHARPE':>9}{'PF':>7}{'FTMO':>6}")
    print(f"{'-'*65}")

    windows = [
        ("Recent 6mo (v8c)",   r1),
        ("Full 12mo",          r2),
        ("Older 6mo",          r3),
    ]
    for name, r in windows:
        print(f"{name:<24}"
              f"{r['return_pct']:>7.2f}%"
              f"{r['win_rate']:>6.1f}%"
              f"{r['trades']:>8}"
              f"{r['max_dd']:>6.2f}%"
              f"{r['sharpe']:>9.3f}"
              f"{r['profit_factor']:>7.3f}"
              f"{'PASS' if r['ftmo_pass'] else 'FAIL':>6}")

    # Count how many windows pass
    passes = sum(1 for _,r in windows if r["ftmo_pass"])
    positives = sum(1 for _,r in windows if r["return_pct"]>0)

    print(f"\n{'='*65}")
    print(f"VERDICT")
    print(f"{'='*65}")
    print(f"\nWindows with positive return: {positives}/3")
    print(f"Windows passing FTMO rules:   {passes}/3")

    if passes == 3:
        verdict = "STRONG — passes all 3 windows. Edge is real across time."
        confidence = "HIGH"
    elif passes == 2 and positives == 3:
        verdict = "GOOD — 2/3 windows pass FTMO, all 3 profitable."
        confidence = "MEDIUM-HIGH"
    elif positives >= 2:
        verdict = "MODERATE — profitable in most windows but risk management needs tuning."
        confidence = "MEDIUM"
    else:
        verdict = "WEAK — only profitable in one window. Edge may not be robust."
        confidence = "LOW"

    print(f"Verdict:    {verdict}")
    print(f"Confidence: {confidence}")

    if confidence in ("HIGH","MEDIUM-HIGH"):
        print(f"\nRECOMMENDATION:")
        print(f"  1. Proceed with FTMO free trial first (ftmo.com → Free Trial)")
        print(f"  2. Then enter $10K challenge (~€155) to prove live execution")
        print(f"  3. Scale to $50K once live performance confirmed")
    else:
        print(f"\nRECOMMENDATION:")
        print(f"  Do NOT pay evaluation fees yet.")
        print(f"  The edge needs to be more consistent across time windows.")

    summary = {
        "type": "backtest_v8d_multi_window",
        "run_date": datetime.now(timezone.utc).isoformat(),
        "windows": {
            "recent_6mo": {k:v for k,v in r1.items() if k!="equity_curve"},
            "full_12mo":  {k:v for k,v in r2.items() if k!="equity_curve"},
            "older_6mo":  {k:v for k,v in r3.items() if k!="equity_curve"},
        },
        "windows_passing": passes,
        "windows_profitable": positives,
        "verdict": verdict,
        "confidence": confidence,
    }

    try:
        sb_url=os.environ.get("SUPABASE_URL")
        sb_key=os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text=(
                f"BACKTEST v8d — MULTI-WINDOW VALIDATION\n\n"
                f"Recent 6mo:  {r1['return_pct']:+.2f}% | "
                f"DD:{r1['max_dd']:.2f}% | FTMO:{'PASS' if r1['ftmo_pass'] else 'FAIL'}\n"
                f"Full 12mo:   {r2['return_pct']:+.2f}% | "
                f"DD:{r2['max_dd']:.2f}% | FTMO:{'PASS' if r2['ftmo_pass'] else 'FAIL'}\n"
                f"Older 6mo:   {r3['return_pct']:+.2f}% | "
                f"DD:{r3['max_dd']:.2f}% | FTMO:{'PASS' if r3['ftmo_pass'] else 'FAIL'}\n\n"
                f"Verdict: {verdict}\nConfidence: {confidence}"
            )
            payload=json.dumps({
                "week_ending": datetime.now(timezone.utc).isoformat(),
                "report_text": report_text,
                "bot_data": summary,
                "news_context": json.dumps({"type":"backtest_v8d"}),
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
