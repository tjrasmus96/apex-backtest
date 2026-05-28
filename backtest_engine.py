"""
APEX BACKTESTING ENGINE v2 — IMPROVED STRATEGY
Fixes applied:
1. Raised signal threshold (3.5+ instead of 2.5) — fewer but higher quality trades
2. Tightened stop losses (1.5x ATR instead of 2.5x) — cut losses faster
3. Added market regime filter (ADX > 25 required) — no trading in choppy markets
4. Added trend confirmation before buying
5. Improved take profit (3x ATR instead of 5x) — lock in gains faster
"""
import json, math, urllib.request, os
from datetime import datetime, timezone

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
            bars.append({"timestamp":ts,
                "open":ohlcv["open"][i] or c,"high":ohlcv["high"][i] or c,
                "low":ohlcv["low"][i] or c,"close":c,"volume":ohlcv["volume"][i] or 0})
        print(f"  {symbol}: {len(bars)} bars")
        return bars
    except Exception as e:
        print(f"  {symbol}: failed — {e}"); return []

def calc_rsi(closes, period=14):
    if len(closes)<period+1: return 50.0
    gains=[max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    losses=[max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag=sum(gains[-period:])/period; al=sum(losses[-period:])/period
    if al==0: return 100.0
    return 100-100/(1+ag/al)

def calc_ema(closes, period):
    if len(closes)<period: return closes[-1]
    k=2/(period+1); ema=sum(closes[:period])/period
    for p in closes[period:]: ema=p*k+ema*(1-k)
    return ema

def calc_bb(closes, period=20):
    if len(closes)<period: c=closes[-1]; return c*1.02,c,c*0.98
    sl=closes[-period:]; mid=sum(sl)/period
    std=math.sqrt(sum((x-mid)**2 for x in sl)/period)
    return mid+2*std, mid, mid-2*std

def calc_adx(closes, period=14):
    if len(closes)<period*2: return 20.0
    tr=[abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
    atr=sum(tr[-period:])/period or 1
    dm_p=[max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    dm_m=[max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    di_p=sum(dm_p[-period:])/period/atr*100
    di_m=sum(dm_m[-period:])/period/atr*100
    if di_p+di_m==0: return 20.0
    return abs(di_p-di_m)/(di_p+di_m)*100

def calc_atr(closes, period=14):
    if len(closes)<2: return closes[-1]*0.02
    trs=[abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
    return sum(trs[-period:])/min(len(trs),period)

def calc_zscore(closes, period=20):
    if len(closes)<period: return 0.0
    sl=closes[-period:]; mid=sum(sl)/period
    std=math.sqrt(sum((x-mid)**2 for x in sl)/period) or 1
    return (closes[-1]-mid)/std

def calc_stoch_rsi(closes, period=14):
    if len(closes)<period*2: return 50.0
    rsi_vals=[]
    for i in range(period, len(closes)):
        gains=[max(closes[j]-closes[j-1],0) for j in range(i-period+1,i+1)]
        losses=[max(closes[j-1]-closes[j],0) for j in range(i-period+1,i+1)]
        ag=sum(gains)/period; al=sum(losses)/period
        rsi_vals.append(100.0 if al==0 else 100-100/(1+ag/al))
    if len(rsi_vals)<period: return 50.0
    recent=rsi_vals[-period:]; mn,mx=min(recent),max(recent)
    if mx==mn: return 50.0
    return (rsi_vals[-1]-mn)/(mx-mn)*100

def get_regime(closes):
    if len(closes)<30: return "UNKNOWN"
    adx=calc_adx(closes)
    if adx>30: return "STRONG_TREND"
    elif adx>25: return "TRENDING"
    elif adx<20: return "RANGING"
    return "NEUTRAL"

def momentum_signal_v2(closes):
    if len(closes)<50: return "HOLD",0
    regime=get_regime(closes)
    if regime=="RANGING": return "HOLD",0
    rsi=calc_rsi(closes)
    e9=calc_ema(closes,9); e21=calc_ema(closes,21)
    e50=calc_ema(closes,min(50,len(closes)))
    macd=calc_ema(closes,12)-calc_ema(closes,26) if len(closes)>=26 else 0
    stoch=calc_stoch_rsi(closes); adx=calc_adx(closes)
    momentum=(closes[-1]-closes[-10])/closes[-10] if len(closes)>=10 else 0
    score=0; agrees=0
    if rsi<25:   score+=3.0; agrees+=1
    elif rsi<35: score+=1.5; agrees+=0.5
    elif rsi>75: score-=3.0; agrees+=1
    elif rsi>65: score-=1.5; agrees+=0.5
    if e9>e21>e50:   score+=2.5; agrees+=1
    elif e9<e21<e50: score-=2.5; agrees+=1
    if macd>0: score+=1.0; agrees+=0.5
    else:       score-=1.0; agrees+=0.5
    if stoch<20:  score+=2.0; agrees+=1
    elif stoch>80: score-=2.0; agrees+=1
    if momentum>0.02:   score+=1.0
    elif momentum<-0.02: score-=1.0
    if adx>30: score*=1.2
    if score>=3.5 and agrees>=2: return "BUY",score
    elif score<=-3.0 and agrees>=2: return "SELL",score
    return "HOLD",score

def mean_rev_signal_v2(closes):
    if len(closes)<30: return "HOLD",0
    adx=calc_adx(closes)
    if adx>35: return "HOLD",0
    upper,mid,lower=calc_bb(closes)
    rsi=calc_rsi(closes); z=calc_zscore(closes)
    stoch=calc_stoch_rsi(closes); cur=closes[-1]
    score=0; agrees=0
    if cur<lower*0.99:   score+=3.5; agrees+=1
    elif cur<lower:       score+=2.0; agrees+=0.5
    elif cur>upper*1.01:  score-=3.5; agrees+=1
    elif cur>upper:       score-=2.0; agrees+=0.5
    if z<-2.5:   score+=2.5; agrees+=1
    elif z<-1.8: score+=1.2; agrees+=0.5
    elif z>2.5:  score-=2.5; agrees+=1
    elif z>1.8:  score-=1.2; agrees+=0.5
    if rsi<20:   score+=2.0; agrees+=1
    elif rsi<30: score+=1.0; agrees+=0.5
    elif rsi>80: score-=2.0; agrees+=1
    elif rsi>70: score-=1.0; agrees+=0.5
    if stoch<15:  score+=1.5; agrees+=0.5
    elif stoch>85: score-=1.5; agrees+=0.5
    if score>=3.5 and agrees>=2: return "BUY",score
    elif score<=-3.5 and agrees>=2: return "SELL",score
    return "HOLD",score

def trend_signal_v2(closes):
    if len(closes)<80: return "HOLD",0
    adx=calc_adx(closes)
    if adx<25: return "HOLD",0
    e20=calc_ema(closes,20); e50=calc_ema(closes,50)
    e100=calc_ema(closes,min(100,len(closes))); cur=closes[-1]
    slope20=(closes[-1]-closes[-20])/closes[-20]
    closes_4h=closes[::4]
    e20_4h=calc_ema(closes_4h,min(20,len(closes_4h)))
    e50_4h=calc_ema(closes_4h,min(50,len(closes_4h)))
    cur_4h=closes_4h[-1]
    four_h_bull=cur_4h>e20_4h>e50_4h
    four_h_bear=cur_4h<e20_4h<e50_4h
    score=0; agrees=0
    if cur>e20>e50>e100:   score+=3.5; agrees+=1
    elif cur>e20>e50:       score+=2.0; agrees+=0.5
    elif cur<e20<e50<e100:  score-=3.5; agrees+=1
    elif cur<e20<e50:       score-=2.0; agrees+=0.5
    if slope20>0.05:    score+=2.0; agrees+=1
    elif slope20>0.02:  score+=1.0; agrees+=0.5
    elif slope20<-0.05: score-=2.0; agrees+=1
    elif slope20<-0.02: score-=1.0; agrees+=0.5
    if four_h_bull:  score+=1.5; agrees+=1
    elif four_h_bear: score-=1.5; agrees+=1
    if adx>35: score*=1.3
    elif adx>30: score*=1.15
    if score>=3.5 and agrees>=2 and four_h_bull: return "BUY",score
    elif score<=-3.0 and agrees>=2 and four_h_bear: return "SELL",score
    return "HOLD",score

class Backtest:
    def __init__(self,name,signal_fn,symbols,cash=100000,stop_atr=1.5,take_atr=3.0):
        self.name=name;self.signal_fn=signal_fn;self.symbols=symbols
        self.cash=cash;self.starting=cash;self.positions={}
        self.trades=[];self.equity_curve=[cash];self.peak=cash
        self.max_dd=0;self.stop_atr=stop_atr;self.take_atr=take_atr

    def run(self,all_bars):
        max_len=max((len(all_bars.get(s,[])) for s in self.symbols),default=0)
        print(f"  [{self.name}] {max_len} bars, {len(self.symbols)} symbols...")
        for i in range(80,max_len):
            total=self.cash
            for sym,pos in self.positions.items():
                bars=all_bars.get(sym,[])
                if i<len(bars): total+=pos["qty"]*bars[i]["close"]
            self.equity_curve.append(total)
            if total>self.peak: self.peak=total
            dd=(self.peak-total)/self.peak*100
            if dd>self.max_dd: self.max_dd=dd
            for sym in self.symbols:
                bars=all_bars.get(sym,[])
                if i>=len(bars): continue
                closes=[b["close"] for b in bars[:i+1]]
                cur=closes[-1];atr=calc_atr(closes)
                sig,score=self.signal_fn(closes);held=sym in self.positions
                if held:
                    pos=self.positions[sym];entry=pos["entry"]
                    if cur<entry-atr*self.stop_atr:
                        profit=(cur-entry)*pos["qty"];self.cash+=cur*pos["qty"]
                        self.trades.append({"symbol":sym,"entry":entry,"exit":cur,
                            "qty":pos["qty"],"profit":profit,
                            "profit_pct":profit/pos["value"]*100,
                            "reason":"Stop loss","bars_held":i-pos["entry_idx"]})
                        del self.positions[sym];continue
                    if cur>entry+atr*self.take_atr:
                        profit=(cur-entry)*pos["qty"];self.cash+=cur*pos["qty"]
                        self.trades.append({"symbol":sym,"entry":entry,"exit":cur,
                            "qty":pos["qty"],"profit":profit,
                            "profit_pct":profit/pos["value"]*100,
                            "reason":"Take profit","bars_held":i-pos["entry_idx"]})
                        del self.positions[sym];continue
                if sig=="BUY" and not held and self.cash>100:
                    spend=min(total*0.05,self.cash*0.25,500)
                    if spend>20:
                        qty=spend/cur;self.cash-=spend
                        self.positions[sym]={"qty":qty,"entry":cur,"entry_idx":i,"value":spend,"score":score}
                elif sig=="SELL" and held:
                    pos=self.positions[sym];profit=(cur-pos["entry"])*pos["qty"]
                    self.cash+=cur*pos["qty"]
                    self.trades.append({"symbol":sym,"entry":pos["entry"],"exit":cur,
                        "qty":pos["qty"],"profit":profit,
                        "profit_pct":profit/pos["value"]*100,
                        "reason":"Signal","bars_held":i-pos["entry_idx"]})
                    del self.positions[sym]
        for sym,pos in list(self.positions.items()):
            bars=all_bars.get(sym,[])
            if not bars: continue
            cur=bars[-1]["close"];profit=(cur-pos["entry"])*pos["qty"]
            self.cash+=cur*pos["qty"]
            self.trades.append({"symbol":sym,"entry":pos["entry"],"exit":cur,
                "qty":pos["qty"],"profit":profit,
                "profit_pct":profit/pos["value"]*100,
                "reason":"End of period","bars_held":max_len-pos["entry_idx"]})

    def results(self):
        final=self.cash;ret=(final-self.starting)/self.starting*100
        wins=[t for t in self.trades if t["profit"]>0]
        losses=[t for t in self.trades if t["profit"]<=0]
        wr=len(wins)/len(self.trades)*100 if self.trades else 0
        avg_win=sum(t["profit"] for t in wins)/len(wins) if wins else 0
        avg_loss=abs(sum(t["profit"] for t in losses)/len(losses)) if losses else 0
        pf=(sum(t["profit"] for t in wins)/abs(sum(t["profit"] for t in losses))
            if losses and wins else 0)
        if len(self.equity_curve)>1:
            rets=[(self.equity_curve[i]-self.equity_curve[i-1])/self.equity_curve[i-1]
                  for i in range(1,len(self.equity_curve))]
            avg_r=sum(rets)/len(rets)
            std_r=math.sqrt(sum((r-avg_r)**2 for r in rets)/len(rets)) if rets else 1
            sharpe=avg_r/std_r*math.sqrt(24*365) if std_r>0 else 0
        else: sharpe=0
        by_sym={}
        for t in self.trades: by_sym[t["symbol"]]=by_sym.get(t["symbol"],0)+t["profit"]
        best=max(by_sym,key=by_sym.get) if by_sym else "N/A"
        worst=min(by_sym,key=by_sym.get) if by_sym else "N/A"
        avg_hold=sum(t["bars_held"] for t in self.trades)/len(self.trades) if self.trades else 0
        return {"strategy":self.name,"starting":self.starting,"final":round(final,2),
            "return_pct":round(ret,2),"profit":round(final-self.starting,2),
            "trades":len(self.trades),"wins":len(wins),"losses":len(losses),
            "win_rate":round(wr,1),"avg_win":round(avg_win,2),"avg_loss":round(avg_loss,2),
            "profit_factor":round(pf,2),"max_dd":round(self.max_dd,2),
            "sharpe":round(sharpe,2),"avg_hold_hrs":round(avg_hold,1),
            "best_symbol":best,"best_pnl":round(by_sym.get(best,0),2),
            "worst_symbol":worst,"worst_pnl":round(by_sym.get(worst,0),2),
            "equity_curve":[round(e,2) for e in self.equity_curve[::20]]}

def run_backtest():
    print("\n"+"="*60)
    print("APEX BACKTEST v2 — IMPROVED STRATEGY")
    print("Fixes: Higher threshold | Tighter stops | Regime filter")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*60)
    bot1_syms=["BTC/USD","ETH/USD","SOL/USD","AVAX/USD","LINK/USD"]
    bot2_syms=["ETH/USD","BCH/USD","LTC/USD","AAVE/USD","UNI/USD"]
    bot3_syms=["BTC/USD","ETH/USD","SOL/USD"]
    all_syms=list(set(bot1_syms+bot2_syms+bot3_syms))
    print(f"\n[1/4] Downloading 6 months of real data...")
    all_bars={}
    for sym in all_syms:
        bars=fetch_historical(sym,months=6)
        if bars: all_bars[sym]=bars
    print(f"  {len(all_bars)} symbols, {sum(len(v) for v in all_bars.values()):,} bars")
    print(f"\n[2/4] Running improved backtests...")
    bt1=Backtest("MOMENTUM_v2",momentum_signal_v2,bot1_syms,stop_atr=1.5,take_atr=3.0)
    bt1.run({k:all_bars.get(k,[]) for k in bot1_syms});r1=bt1.results()
    bt2=Backtest("MEAN_REV_v2",mean_rev_signal_v2,bot2_syms,stop_atr=1.5,take_atr=3.0)
    bt2.run({k:all_bars.get(k,[]) for k in bot2_syms});r2=bt2.results()
    bt3=Backtest("TREND_v2",trend_signal_v2,bot3_syms,stop_atr=1.5,take_atr=3.0)
    bt3.run({k:all_bars.get(k,[]) for k in bot3_syms});r3=bt3.results()
    results=[r1,r2,r3]
    total_start=300000;total_final=r1["final"]+r2["final"]+r3["final"]
    total_ret=(total_final-total_start)/total_start*100
    total_trades=r1["trades"]+r2["trades"]+r3["trades"]
    total_wins=r1["wins"]+r2["wins"]+r3["wins"]
    combined_wr=total_wins/total_trades*100 if total_trades else 0
    worst_dd=max(r["max_dd"] for r in results)
    avg_pf=sum(r["profit_factor"] for r in results)/3
    prop_ready=total_ret>=10 and worst_dd<10
    summary={"type":"backtest_v2","fixes":["higher_threshold","tighter_stops","regime_filter"],
        "period":"6 months","run_date":datetime.now(timezone.utc).isoformat(),
        "total_start":total_start,"total_final":round(total_final,2),
        "total_return_pct":round(total_ret,2),"total_profit":round(total_final-total_start,2),
        "total_trades":total_trades,"combined_win_rate":round(combined_wr,1),
        "worst_drawdown":round(worst_dd,2),"avg_profit_factor":round(avg_pf,2),
        "prop_firm_ready":prop_ready,"strategies":results}
    print(f"\n[3/4] RESULTS — v2 IMPROVED")
    print(f"\n{'STRATEGY':<16}{'RETURN':>9}{'WIN%':>7}{'TRADES':>8}{'DD%':>7}{'SHARPE':>8}{'PF':>6}")
    print("-"*62)
    for r in results:
        print(f"{r['strategy']:<16}{r['return_pct']:>8.1f}%{r['win_rate']:>6.1f}%"
              f"{r['trades']:>8}{r['max_dd']:>6.1f}%{r['sharpe']:>8.2f}{r['profit_factor']:>6.2f}")
    print(f"\n{'COMBINED':<16}{total_ret:>8.1f}%{combined_wr:>6.1f}%{total_trades:>8}{worst_dd:>6.1f}%")
    print(f"\n$300K → ${total_final:,.0f} | Profit: ${total_final-total_start:+,.0f}")
    print(f"Avg Profit Factor: {avg_pf:.2f}")
    print(f"\nProp Firm Ready: {'YES — Apply Now!' if prop_ready else 'NOT YET'}")
    if not prop_ready:
        if total_ret<10: print(f"  Need {10-total_ret:.1f}% more return")
        if worst_dd>=10: print(f"  Drawdown {worst_dd:.1f}% too high")
    for r in results:
        print(f"\n  {r['strategy']}: {r['return_pct']:+.1f}% | WR:{r['win_rate']:.0f}% | "
              f"Holds avg {r['avg_hold_hrs']:.0f}hrs | Best:{r['best_symbol']} ${r['best_pnl']:+,.0f}")
    print(f"\n[4/4] Saving...")
    try:
        sb_url=os.environ.get("SUPABASE_URL");sb_key=os.environ.get("SUPABASE_KEY")
        if sb_url and sb_key:
            report_text=(f"BACKTEST v2 — IMPROVED STRATEGY\n\nFIXES: Higher threshold | Tighter stops | Regime filter\n\n"
                f"Total Return: {total_ret:+.1f}% | Win Rate: {combined_wr:.0f}% | Max DD: {worst_dd:.1f}% | Trades: {total_trades}\n"
                f"Prop Firm Ready: {'YES' if prop_ready else 'NOT YET'}\n\n"
                f"MOMENTUM_v2: {r1['return_pct']:+.1f}% | WR:{r1['win_rate']:.0f}% | {r1['trades']} trades\n"
                f"MEAN_REV_v2: {r2['return_pct']:+.1f}% | WR:{r2['win_rate']:.0f}% | {r2['trades']} trades\n"
                f"TREND_v2: {r3['return_pct']:+.1f}% | WR:{r3['win_rate']:.0f}% | {r3['trades']} trades")
            payload=json.dumps({"week_ending":datetime.now(timezone.utc).isoformat(),
                "report_text":report_text,"bot_data":summary,
                "news_context":json.dumps({"type":"backtest_v2"})}).encode()
            req=urllib.request.Request(f"{sb_url}/rest/v1/reports",data=payload,
                headers={"Content-Type":"application/json","apikey":sb_key,
                         "Authorization":f"Bearer {sb_key}","Prefer":"return=minimal"},method="POST")
            with urllib.request.urlopen(req,timeout=15) as r:
                print(f"  Saved to Supabase!")
    except Exception as e: print(f"  Failed: {e}")
    with open("backtest_results.json","w") as f: json.dump(summary,f,indent=2,default=str)
    print(f"  Saved backtest_results.json")
    return summary

if __name__=="__main__":
    run_backtest()
