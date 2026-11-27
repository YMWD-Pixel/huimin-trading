#!/usr/bin/env python3
"""Huimin trading bot v1.5 — Binance USDT-margined futures, ETHUSDT only.
Style: 熬鹰 (see trading-style-aoying.md + aoying-full-records-20261127.md)
+ 法师 Tony (see strategy-research-20261127.md):
  - event-driven / range "打地鼠": fade the edges of the recent 24h range,
    only when momentum is NOT running away (no breakout chasing);
  - low leverage (3x), every trade has a stop-loss, bank profits;
  - no setup = no trade. Empty positions are also a position.
Risk rules (hard):
  - max 2% of balance risked per trade; stop-loss on every trade;
  - daily realized loss cap $5 (bot's own trades only, v1.1)
    -> no new trades that day. v1 counted the whole account's
    ETHUSDT realized PnL, so YM's manual trades could block the bot
    for a day even when the bot lost nothing; v1.1 attributes PnL
    per bot trade instead (income snapshot at entry vs at close,
    tracked in state.json) and only counts that.
  - v1.2: hedge-mode aware. YM's account is dualSidePosition=True, so
    every order must carry positionSide LONG/SHORT or Binance rejects
    it with -4061. The bot reads the mode at startup and adds
    positionSide only in hedge mode (one-way accounts unchanged).
  - v1.3: stop-loss via the Algo Order API. Binance moved all
    conditional orders (STOP_MARKET etc.) off /fapi/v1/order on
    2025-12-09; the old endpoint now rejects them with -4120, which
    is why the 2026-11-27 short ran with NO working stop. Stops now
    go to POST /fapi/v1/algoOrder (algoType=CONDITIONAL,
    triggerPrice, closePosition=true) and are verified against
    GET /fapi/v1/openAlgoOrders (algo orders are invisible in
    openOrders). Fail-safe: if a stop cannot be placed, the
    position is closed immediately instead of being held naked; a
    position found without a stop is closed after 3 consecutive
    failed placement attempts.
  - v1.3: weekly-trend filter (YM 2026-11-27: don't short a weekly
    uptrend). Trend = current price vs the close 7 days ago
    (daily klines, +/-1% band = flat). No SHORT when weekly is up,
    no LONG when weekly is down. On 2026-11-27 the 7-day change
    was +6.7%, so the 10:07 short would have been blocked.
  - v1.3: wins/losses are now counted on close (v1.2 never did).
  - v1.4 (YM 2026-11-27: all 7 research items): regime filter —
    range-fade only when |7d change| <= 2%, trend markets only do
    pullback entries WITH the trend (TP 2R); momentum confirmation
    (entry hour must agree, skip wild 1h moves > 2%); staged
    take-profit (stop to breakeven at +1R, take half at +1.5R,
    trail the rest 0.5R behind the peak); scaled entries (half now,
    half at +0.5R); loss circuit breakers (risk halves after 2
    consecutive losses, weekly PnL -$15 halts until Monday);
    ATR-adaptive stops; event-day blacklist (no new entries).
  - v1.5 (YM 2026-11-27: all 8 熬鹰 items): withdrawal rule —
    recommend withdrawing each $25 step above $100 when the week
    is profitable (bot never moves funds itself); one-sentence
    trade thesis logged at entry and reviewed at close; confirmed
    scale-in (second half only after two consecutive 1h closes
    beyond +0.5R); trade expiry (12h chop / 24h trend without
    +1R -> exit); risk ladder by balance (2% <$100, 1.5% $100-500,
    1% >$500, halved again after 2 consecutive losses); no-story
    day frequency cut (narrow chop days: watch only); conditional
    reversal (cooldown exception for a confirmed breakout after a
    failed fade); momentum-reversal early exit (drop 0.75R from
    peak before breakeven -> exit). NOT copied: 20x all-in,
    cross-market rotation, no-stop wire-walking, holding through
    drawdowns, news-direction bets (no news feed).
  - balance < $50 -> halt everything and log;
  - max 1 open position; 2h cooldown after a close.
Stdlib only. Reads key/secret from ~/.binance/credentials (mode 600).
State: state.json ; trade log: trades.log ; kill switch: create file STOP
in this directory to pause new trades (open positions keep their stops).
"""
import hashlib
import hmac
import json
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://fapi.binance.com"
CREDS = os.path.expanduser("~/.binance/credentials")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "trades.log")
STOP_FILE = os.path.join(HERE, "STOP")

SYMBOL = "ETHUSDT"
LEVERAGE = 3
RISK_PCT = 0.02          # max risk per trade, fraction of balance
DAILY_LOSS_CAP = 5.0     # USD
STOP_BALANCE = 50.0      # halt below this
COOLDOWN_SEC = 2 * 3600
CHECK_SEC = 60
QTY_STEP = 0.001
PRICE_TICK = 0.01
MIN_NOTIONAL = 5.0
REGIME_CHG = 0.02        # |7d change| above this = trend market, else chop
WEEKLY_LOSS_CAP = 15.0  # halt new trades for the week at this bot PnL
WITHDRAW_BASE = 100.0   # keep this much working capital; recommend
WITHDRAW_STEP = 25.0    # withdrawing each $25 step above it (v1.5 #1)
# UTC dates with no new entries (FOMC/CPI etc.). Update monthly.
NO_TRADE_DATES = {"2026-12-09", "2026-12-10"}


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_creds():
    creds = {}
    with open(CREDS) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    return creds["BINANCE_API_KEY"], creds["BINANCE_API_SECRET"]


KEY, SECRET = load_creds()

# Set at startup from GET /fapi/v1/positionSide/dual. In hedge mode every
# order must carry positionSide LONG/SHORT, else Binance rejects it -4061.
HEDGE = False


def _ps(side):
    """positionSide param for an order, only in hedge mode."""
    return {"positionSide": side} if HEDGE else {}


def public(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def signed(path, params=None, method="GET"):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, method=method,
                                 headers={"X-MBX-APIKEY": KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} {path}: {body}")


def utc_date_str(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_close_ts": 0, "trades": 0, "wins": 0, "losses": 0,
                "bot_pnl_date": utc_date_str(), "bot_pnl_today": 0.0,
                "entry_pnl": None, "entry_date": None,
                "had_position": False, "open_stop": None, "open_tp": None,
                "open_side": None, "stop_fail_streak": 0,
                "open_dist": None, "open_qty_full": None,
                "scale_done": False, "tp_stage": 0, "trail_peak_r": 0.0,
                "stop_algo_id": None, "consec_losses": 0,
                "weekly_pnl": 0.0, "weekly_pnl_week": None,
                "open_ts": None, "open_regime": None, "open_thesis": None,
                "peak_r": 0.0, "close_reason": None,
                "last_close_side": None, "last_close_regime": None,
                "last_close_loss": None, "last_close_reason": None,
                "last_withdraw_threshold": 100.0}


def rollover_bot_pnl(state):
    """Reset the bot-attributed daily PnL at UTC midnight.
    Keeps entry_pnl/entry_date so a position open across midnight is
    still detected on close (its PnL is then left unattributed rather
    than misattributed — see the close handler)."""
    today = utc_date_str()
    if state.get("bot_pnl_date") != today:
        state["bot_pnl_date"] = today
        state["bot_pnl_today"] = 0.0
    return state.get("bot_pnl_today", 0.0)


def week_key(ts=None):
    return time.strftime("%Y-W%W", time.gmtime(ts))


def weekly_halt(state):
    """True if this week's bot PnL is at/below the -$15 cap (v1.4 #5)."""
    if state.get("weekly_pnl_week") != week_key():
        return False
    return state.get("weekly_pnl", 0.0) <= -WEEKLY_LOSS_CAP


def risk_pct_for(balance, state):
    """Risk per trade, by balance ladder (v1.5 #5, 熬鹰: the bigger
    the money, the lower the leverage). Halved again after 2
    consecutive losses (v1.4 #5)."""
    if balance >= 500:
        base = 0.01
    elif balance >= 100:
        base = 0.015
    else:
        base = 0.02
    if state.get("consec_losses", 0) >= 2:
        base /= 2
    return base


def check_withdrawal(state, balance):
    """v1.5 #1 (熬鹰: 提现才算赚到). When the balance crosses each
    $25 step above $100 with a positive week, log a WITHDRAW
    RECOMMEND. The bot never moves funds itself — withdrawal is
    manual, YM approves. Logged so the agent picks it up during
    checks and writes it into the ledger."""
    if balance <= WITHDRAW_BASE:
        return
    threshold = (WITHDRAW_BASE
                 + WITHDRAW_STEP * int((balance - WITHDRAW_BASE)
                                       // WITHDRAW_STEP))
    if threshold <= state.get("last_withdraw_threshold", 100.0):
        return
    if state.get("weekly_pnl", 0.0) <= 0:
        return
    state["last_withdraw_threshold"] = threshold
    log(f"WITHDRAW RECOMMEND: balance ${balance:.2f} crossed "
        f"${threshold:.0f}; consider withdrawing "
        f"${balance - WITHDRAW_BASE:.2f} (keep "
        f"${WITHDRAW_BASE:.0f} working capital). "
        f"Withdrawal is manual — the bot never moves funds itself.")
    save_state(state)


def check_reversal(state, rg):
    """v1.5 #7: after a chop fade stop-out, if the price has closed
    beyond the broken edge by > 0.5%, the fade failed and the
    breakout is real. Returns the breakout side ('LONG'/'SHORT')
    or None. The price here is the latest 1h candle close."""
    if not state.get("last_close_loss"):
        return None
    if state.get("last_close_regime") != "chop":
        return None
    last_side = state.get("last_close_side")
    price = rg["price"]
    if last_side == "SHORT" and price >= rg["high24"] * 1.005:
        return "LONG"
    if last_side == "LONG" and price <= rg["low24"] * 0.995:
        return "SHORT"
    return None


def clear_trade_state(state):
    """Reset all per-trade fields (on close or aborted entry).
    last_close_* are NOT cleared — the next trade may need them
    (v1.5 #7 reversal exception)."""
    state["entry_pnl"] = None
    state["entry_date"] = None
    state["open_stop"] = None
    state["open_tp"] = None
    state["open_side"] = None
    state["open_dist"] = None
    state["open_qty_full"] = None
    state["scale_done"] = False
    state["tp_stage"] = 0
    state["trail_peak_r"] = 0.0
    state["stop_algo_id"] = None
    state["stop_fail_streak"] = 0
    state["open_ts"] = None
    state["open_regime"] = None
    state["open_thesis"] = None
    state["peak_r"] = 0.0
    state["close_reason"] = None


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


def round_down(x, step):
    return int(x / step) * step


def get_balance():
    bal = signed("/fapi/v2/balance")
    for b in bal:
        if b["asset"] == "USDT":
            return float(b["balance"]), float(b["availableBalance"])
    return 0.0, 0.0


def get_position():
    pos = signed("/fapi/v2/positionRisk", {"symbol": SYMBOL})
    # hedge mode returns one entry per side; one-way returns a single entry
    for p in pos:
        amt = float(p.get("positionAmt", 0))
        if amt != 0:
            ps = p.get("positionSide")
            side = ps if ps in ("LONG", "SHORT") else (
                "LONG" if amt > 0 else "SHORT")
            return {"amt": amt, "entry": float(p.get("entryPrice", 0)),
                    "unrealized": float(p.get("unRealizedProfit", 0)),
                    "lev": p.get("leverage"), "side": side}
    return {"amt": 0.0, "entry": 0.0, "unrealized": 0.0,
            "lev": None, "side": None}


def get_open_orders():
    return signed("/fapi/v1/openOrders", {"symbol": SYMBOL})


def cancel_all():
    try:
        signed("/fapi/v1/allOpenOrders", {"symbol": SYMBOL}, method="DELETE")
        log("cancelled all open orders")
    except Exception as e:
        log(f"cancel failed: {e}")
    try:
        signed("/fapi/v1/algoOpenOrders", {"symbol": SYMBOL}, method="DELETE")
        log("cancelled all open algo orders")
    except Exception as e:
        log(f"algo cancel failed: {e}")


def atr_from_klines(kl, period=14):
    """Average True Range from klines (each row: [ts, o, h, l, c, ...])."""
    trs = []
    for i in range(1, len(kl)):
        h, l = float(kl[i][2]), float(kl[i][3])
        pc = float(kl[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else 0.0


def get_range():
    kl = public("/fapi/v1/klines",
                {"symbol": SYMBOL, "interval": "1h", "limit": 24})
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    return {"high24": max(highs), "low24": min(lows),
            "last_close": closes[-1], "prev_close": closes[-2],
            "price": closes[-1], "cur_open": float(kl[-1][1]),
            "atr": atr_from_klines(kl), "closes": closes}


def get_weekly_change():
    """7-day price change (fraction), or None if it cannot be fetched.
    Daily klines, limit 8: klines[-1] is today (close = latest
    price), klines[-8] is the close 7 days ago. On 2026-11-27 the
    7-day change was +6.7% (1623.09 -> 1731.73)."""
    try:
        kl = public("/fapi/v1/klines",
                    {"symbol": SYMBOL, "interval": "1d", "limit": 8})
        week_ago = float(kl[-8][4])
        now = float(kl[-1][4])
        return now / week_ago - 1
    except Exception as e:
        log(f"weekly change fetch failed: {e}")
        return None


def r_multiple(side, entry, price, dist):
    """How many R the position is in profit (+ = winning)."""
    if not dist:
        return 0.0
    if side == "LONG":
        return (price - entry) / dist
    return (entry - price) / dist


def trail_stop_price(side, entry, dist, peak_r):
    """Trailing stop 0.5R behind the best R reached (never moves back)."""
    if side == "LONG":
        return entry + (peak_r - 0.5) * dist
    return entry - (peak_r - 0.5) * dist


def today_realized_pnl():
    # UTC midnight
    now = int(time.time())
    start = (now - now % 86400) * 1000
    try:
        inc = signed("/fapi/v1/income",
                     {"symbol": SYMBOL, "incomeType": "REALIZED_PNL",
                      "startTime": start, "limit": 1000})
        return sum(float(x["income"]) for x in inc)
    except Exception as e:
        log(f"income check failed: {e}")
        return 0.0


def place_order(**params):
    return signed("/fapi/v1/order", params, method="POST")


def place_algo_order(**params):
    return signed("/fapi/v1/algoOrder", params, method="POST")


def get_open_algo_orders():
    r = signed("/fapi/v1/openAlgoOrders", {"symbol": SYMBOL})
    if not isinstance(r, list):
        log(f"open algo orders unexpected response: {r}")
        return []
    return r


def place_stop(side, stop_price):
    """Stop-loss via the Algo Order API (v1.3).
    Binance moved STOP_MARKET off /fapi/v1/order on 2025-12-09;
    the old endpoint rejects it with -4120. Algo orders need
    algoType=CONDITIONAL and triggerPrice (not stopPrice), and they
    are invisible in /fapi/v1/openOrders — see get_open_algo_orders.
    Returns the algo order response (algoId) or raises."""
    exit_side = "SELL" if side == "LONG" else "BUY"
    s = place_algo_order(algoType="CONDITIONAL", symbol=SYMBOL,
                         side=exit_side, type="STOP_MARKET",
                         triggerPrice=f"{stop_price:.2f}",
                         closePosition="true", workingType="MARK_PRICE",
                         timeInForce="GTC", **_ps(side))
    log(f"  STOP placed algoId={s.get('algoId')} stop={stop_price:.2f}")
    return s


def close_market(side, qty):
    """Market-close a position (fail-safe). side: LONG or SHORT."""
    exit_side = "SELL" if side == "LONG" else "BUY"
    o = place_order(symbol=SYMBOL, side=exit_side, type="MARKET",
                    quantity=f"{qty:.3f}", reduceOnly="true", **_ps(side))
    log(f"  MARKET CLOSE {side} qty={qty:.3f} "
        f"orderId={o.get('orderId')} status={o.get('status')}")
    return o


def cancel_order(order_id):
    return signed("/fapi/v1/order",
                  {"symbol": SYMBOL, "orderId": order_id}, method="DELETE")


def cancel_algo_order(algo_id):
    return signed("/fapi/v1/algoOrder",
                  {"symbol": SYMBOL, "algoId": algo_id}, method="DELETE")


def find_tp_order(orders, exit_side):
    for o in orders:
        if (o.get("type") == "LIMIT" and o.get("side") == exit_side
                and o.get("reduceOnly") in (True, "true")):
            return o
    return None


def place_tp_limit(side, qty, tp_price):
    """Take-profit: reduceOnly limit (regular endpoint, unaffected by
    the -4120 algo migration)."""
    exit_side = "SELL" if side == "LONG" else "BUY"
    t = place_order(symbol=SYMBOL, side=exit_side, type="LIMIT",
                    price=f"{tp_price:.2f}", quantity=f"{qty:.3f}",
                    timeInForce="GTC", reduceOnly="true", **_ps(side))
    log(f"  TP placed orderId={t.get('orderId')} tp={tp_price:.2f} "
        f"qty={qty:.3f}")
    return t


def replace_stop(state, side, new_stop):
    """Move the stop: cancel the old algo stop, place a new one.
    If the cancel succeeds but the place fails, the position is
    briefly unprotected — the manage loop re-places it next minute
    at the stored (old, wider) level. Returns True if the new stop
    is in place."""
    old_id = state.get("stop_algo_id")
    if old_id:
        try:
            cancel_algo_order(old_id)
            log(f"  old stop algoId={old_id} cancelled")
        except Exception as e:
            log(f"  old stop cancel failed (old stop stays): {e}")
    try:
        s = place_stop(side, round(float(new_stop), 2))
        state["stop_algo_id"] = s.get("algoId")
        state["open_stop"] = round(float(new_stop), 2)
        return True
    except Exception as e:
        log(f"  stop replace FAILED: {e}")
        return False


def open_trade(side, qty, stop_price, tp_price, state):
    """side: LONG or SHORT. Places market entry + algo stop + TP.
    Returns (avg_price, stop_ok). If the stop cannot be placed, the
    position is closed immediately — a trade without a stop is not
    held, ever (2026-11-27 lesson)."""
    entry_side = "BUY" if side == "LONG" else "SELL"
    o = place_order(symbol=SYMBOL, side=entry_side, type="MARKET",
                    quantity=f"{qty:.3f}", **_ps(side))
    log(f"ENTRY {side} qty={qty:.3f} orderId={o.get('orderId')} "
        f"status={o.get('status')}")
    fills = o.get("fills", [])
    avg = (sum(float(f["price"]) * float(f["qty"]) for f in fills)
           / sum(float(f["qty"]) for f in fills)) if fills else None
    log(f"  entry avg price: {avg}")
    # stop-loss: Algo Order API, closePosition STOP_MARKET on mark price
    stop_ok = False
    stop_resp = None
    for attempt in (1, 2):
        try:
            stop_resp = place_stop(side, stop_price)
            stop_ok = True
            break
        except Exception as e:
            log(f"  STOP PLACEMENT FAILED (attempt {attempt}): {e}")
            if attempt == 1:
                time.sleep(3)
    if stop_ok and stop_resp is not None:
        state["stop_algo_id"] = stop_resp.get("algoId")
    if not stop_ok:
        # fail-safe: no stop -> no position. Close immediately.
        log("  CRITICAL: stop could not be placed, closing position now")
        try:
            close_market(side, qty)
        except Exception as e:
            log(f"  CRITICAL: emergency close FAILED: {e} "
                f"(position left open, loop will keep trying)")
        return avg, False
    # take-profit: reduceOnly limit (regular endpoint, unaffected)
    try:
        place_tp_limit(side, qty, tp_price)
    except Exception as e:
        log(f"  TP PLACEMENT FAILED: {e} (loop will re-place it)")
    return avg, True


def manage_open_position(pos, orders, algo_orders, state, price, rg=None):
    """Manage an open position: stop protection, TP sync, scale-in,
    staged take-profit (breakeven -> half -> trailing stop).
    price may be None if the price fetch failed — then only the
    stop check runs and the R-based logic is skipped this minute.
    rg is the get_range() dict (needed for the scale-in candle
    confirmation, v1.5 #3)."""
    side = pos["side"]
    exit_side = "SELL" if pos["amt"] > 0 else "BUY"
    amt = abs(pos["amt"])
    entry = pos["entry"]
    dist = state.get("open_dist") or 0.0
    # --- stop protection (v1.3, unchanged). Algo stops are invisible
    # in openOrders — check both lists.
    has_stop = (
        any(o.get("type") == "STOP_MARKET" for o in orders)
        or any(o.get("type") in ("STOP_MARKET", "STOP")
               and o.get("side") == exit_side
               for o in algo_orders))
    if not has_stop:
        log(f"WARNING: open position {pos['amt']} but NO stop "
            f"order! entry={entry} unrealized={pos['unrealized']:.2f}")
        stop = state.get("open_stop") or (
            entry * 0.98 if pos["amt"] > 0 else entry * 1.02)
        try:
            s = place_stop(side, round(float(stop), 2))
            state["stop_algo_id"] = s.get("algoId")
            state["stop_fail_streak"] = 0
            save_state(state)
            log(f"  emergency stop placed at {stop:.2f}")
        except Exception as e:
            streak = state.get("stop_fail_streak", 0) + 1
            state["stop_fail_streak"] = streak
            save_state(state)
            log(f"  emergency stop FAILED (streak {streak}): {e}")
            if streak >= 3:
                # fail-safe: a position that cannot be protected is
                # closed, not held naked.
                log("  CRITICAL: no stop after 3 attempts, "
                    "closing position now")
                try:
                    close_market(side, amt)
                    state["stop_fail_streak"] = 0
                    save_state(state)
                except Exception as e2:
                    log(f"  CRITICAL: emergency close FAILED: {e2}")
            return
    if price is None or not dist:
        return
    # how far in profit, in R
    r = r_multiple(side, entry, price, dist)
    stage = state.get("tp_stage") or 0
    # track the best R reached since entry (v1.5 #8)
    peak_r = max(state.get("peak_r") or 0.0, r)
    if peak_r > (state.get("peak_r") or 0.0):
        state["peak_r"] = peak_r
        save_state(state)
    # --- trade expiry (v1.5 #4): chop 12h / trend 24h without +1R
    if stage == 0 and state.get("open_ts"):
        age_h = (time.time() - state["open_ts"]) / 3600
        limit_h = 24 if state.get("open_regime") == "trend" else 12
        if age_h > limit_h:
            log(f"  EXPIRY: {age_h:.1f}h without +1R, closing")
            state["close_reason"] = "expiry"
            save_state(state)
            try:
                close_market(side, amt)
            except Exception as e:
                log(f"  EXPIRY close FAILED: {e}")
            return
    # --- momentum-reversal early exit (v1.5 #8): once the trade was
    # +0.5R in profit, a 0.75R give-back means it is not going our
    # way (proxy for 熬鹰's "感觉不对就平"). Only before breakeven.
    if stage == 0 and peak_r >= 0.5 and r <= peak_r - 0.75:
        log(f"  REVERSAL EXIT: R fell from peak {peak_r:.2f} to "
            f"{r:.2f}, closing")
        state["close_reason"] = "reversal"
        save_state(state)
        try:
            close_market(side, amt)
        except Exception as e:
            log(f"  REVERSAL close FAILED: {e}")
        return
    # --- scale-in (v1.4 #4, v1.5 #3): add the second half only after
    # TWO consecutive completed 1h closes beyond +0.5R (confirmed,
    # not merely touched). closes[-1] is the current (incomplete)
    # hour, so use closes[-2] and closes[-3]. If the trade first
    # goes -0.5R, no add.
    tp_synced = False
    if not state.get("scale_done") and stage == 0:
        full_qty = state.get("open_qty_full") or amt
        confirmed = False
        closes = (rg or {}).get("closes") or []
        if len(closes) >= 3 and dist:
            if side == "LONG":
                confirmed = (closes[-2] >= entry + 0.5 * dist
                             and closes[-3] >= entry + 0.5 * dist)
            else:
                confirmed = (closes[-2] <= entry - 0.5 * dist
                             and closes[-3] <= entry - 0.5 * dist)
        if confirmed and amt < full_qty - 1e-9:
            add = round_down(full_qty - amt, QTY_STEP)
            if add > 0 and add * price >= MIN_NOTIONAL:
                try:
                    entry_side = "BUY" if side == "LONG" else "SELL"
                    o = place_order(symbol=SYMBOL, side=entry_side,
                                    type="MARKET",
                                    quantity=f"{add:.3f}", **_ps(side))
                    log(f"  SCALE-IN {side} +{add:.3f} "
                        f"orderId={o.get('orderId')} at R={r:.2f}")
                    state["scale_done"] = True
                    # TP quantity must cover the full position now
                    tp_o = find_tp_order(orders, exit_side)
                    if tp_o:
                        try:
                            cancel_order(tp_o.get("orderId"))
                        except Exception as e:
                            log(f"  TP cancel for re-size failed: {e}")
                    if state.get("open_tp"):
                        try:
                            place_tp_limit(side, full_qty,
                                           float(state["open_tp"]))
                            tp_synced = True
                        except Exception as e:
                            log(f"  TP re-place after scale-in "
                                f"FAILED: {e}")
                    save_state(state)
                except Exception as e:
                    log(f"  SCALE-IN FAILED: {e}")
        elif r <= -0.5:
            state["scale_done"] = True
            save_state(state)
    # --- staged take-profit (v1.4 #3)
    # stage 0 -> 1: at +1R move the stop to breakeven
    if stage == 0 and r >= 1.0:
        if replace_stop(state, side, entry):
            state["tp_stage"] = 1
            state["trail_peak_r"] = max(
                state.get("trail_peak_r") or 0.0, r)
            save_state(state)
            log(f"  STAGE 1: stop moved to breakeven {entry:.2f} "
                f"at R={r:.2f}")
            stage = 1
    # stage 1 -> 2: at +1.5R take half off, trail the rest
    if (state.get("tp_stage") or 0) == 1 and r >= 1.5:
        half = round_down(amt / 2, QTY_STEP)
        if half > 0 and half * price >= MIN_NOTIONAL:
            try:
                close_market(side, half)
                state["tp_stage"] = 2
                state["trail_peak_r"] = max(
                    state.get("trail_peak_r") or 0.0, r)
                # no fixed TP anymore; the trailing stop rides it
                tp_o = find_tp_order(orders, exit_side)
                if tp_o:
                    try:
                        cancel_order(tp_o.get("orderId"))
                        log("  TP cancelled, trailing stop rides")
                    except Exception as e:
                        log(f"  TP cancel failed: {e}")
                save_state(state)
                log(f"  STAGE 2: took half off ({half:.3f}) "
                    f"at R={r:.2f}")
            except Exception as e:
                log(f"  STAGE 2 half-close FAILED: {e}")
    # stage 2: trail the stop 0.5R behind the best R reached
    if (state.get("tp_stage") or 0) == 2:
        peak = max(state.get("trail_peak_r") or 0.0, r)
        if peak > (state.get("trail_peak_r") or 0.0):
            state["trail_peak_r"] = peak
            save_state(state)
        new_stop = trail_stop_price(side, entry, dist, peak)
        cur_stop = state.get("open_stop")
        better = (new_stop > cur_stop if side == "LONG"
                  else new_stop < cur_stop) if cur_stop else True
        if better:
            if replace_stop(state, side, new_stop):
                save_state(state)
                log(f"  TRAIL: stop moved to {new_stop:.2f} "
                    f"(peak R={peak:.2f})")
    # --- TP sync (stage 0/1 only): a TP limit must exist for the
    # full current size at the stored level. Skipped if the scale-in
    # block already re-placed it this minute (orders list is stale).
    if (not tp_synced and (state.get("tp_stage") or 0) < 2
            and state.get("open_tp")):
        tp_o = find_tp_order(orders, exit_side)
        if tp_o is None:
            try:
                place_tp_limit(side, amt, float(state["open_tp"]))
            except Exception as e:
                log(f"  TP re-place FAILED: {e}")
        else:
            try:
                have_qty = float(tp_o.get("origQty", 0) or 0)
            except Exception:
                have_qty = 0
            if have_qty + 1e-9 < amt:
                try:
                    cancel_order(tp_o.get("orderId"))
                    place_tp_limit(side, amt, float(state["open_tp"]))
                except Exception as e:
                    log(f"  TP re-size FAILED: {e}")


def main():
    global HEDGE
    acct = signed("/fapi/v2/account")
    log(f"bot v1.5 start | canTrade={acct.get('canTrade')} "
        f"wallet={acct.get('totalWalletBalance')}")
    if not acct.get("canTrade"):
        log("FATAL: canTrade is False, exiting")
        return
    try:
        mode = signed("/fapi/v1/positionSide/dual")
        HEDGE = bool(mode.get("dualSidePosition"))
        log(f"position mode: {'hedge (dualSidePosition=True)' if HEDGE else 'one-way'}")
    except Exception as e:
        log(f"position-mode check failed, assuming one-way: {e}")
    try:
        signed("/fapi/v1/leverage",
               {"symbol": SYMBOL, "leverage": LEVERAGE}, method="POST")
        log(f"leverage set to {LEVERAGE}x on {SYMBOL}")
    except Exception as e:
        log(f"leverage set failed (may already be {LEVERAGE}x): {e}")

    state = load_state()
    heartbeat = 0
    while True:
        try:
            heartbeat += 1
            if os.path.exists(STOP_FILE):
                log("STOP file present: pausing new trades (stops stay on)")
                time.sleep(300)
                continue

            balance, available = get_balance()
            if balance < STOP_BALANCE:
                log(f"HALT: balance ${balance:.2f} < ${STOP_BALANCE} "
                    f"-> no trading, will keep checking")
                time.sleep(3600)
                continue
            # withdrawal recommendation (v1.5 #1)
            check_withdrawal(state, balance)

            pos = get_position()
            orders = get_open_orders()
            try:
                algo_orders = get_open_algo_orders()
            except Exception as e:
                log(f"open algo orders fetch failed: {e}")
                algo_orders = []

            if pos["amt"] == 0:
                # flat: cancel any leftover stop/TP orders
                if orders or algo_orders:
                    cancel_all()
                # cooldown after a close — with a reversal exception
                # (v1.5 #7): a confirmed breakout after a failed fade
                # may be traded immediately
                reversal = None
                if time.time() - state.get("last_close_ts", 0) < COOLDOWN_SEC:
                    try:
                        reversal = check_reversal(state, get_range())
                    except Exception as e:
                        log(f"reversal check failed: {e}")
                        reversal = None
                    if not reversal:
                        time.sleep(CHECK_SEC)
                        continue
                # daily loss cap — bot's own PnL only (v1.1). YM's manual
                # trades no longer block the bot; account PnL is logged
                # for reference only.
                acct_pnl_today = today_realized_pnl()
                bot_pnl = rollover_bot_pnl(state)
                if bot_pnl <= -DAILY_LOSS_CAP:
                    log(f"daily loss cap hit: bot pnl today "
                        f"${bot_pnl:.2f} (account ${acct_pnl_today:.2f}) "
                        f"-> no new trades today")
                    time.sleep(3600)
                    continue
                # event days (v1.4 #7): no new entries on FOMC/CPI etc.
                if utc_date_str() in NO_TRADE_DATES:
                    if heartbeat % 30 == 0:
                        log(f"event day {utc_date_str()}: no new entries")
                    time.sleep(CHECK_SEC)
                    continue
                # weekly loss cap (v1.4 #5): -$15/week -> halt to Monday
                if weekly_halt(state):
                    if heartbeat % 30 == 0:
                        log(f"weekly loss cap hit: pnl "
                            f"${state.get('weekly_pnl', 0.0):.2f} "
                            f"-> no new trades this week")
                        if balance > WITHDRAW_BASE:
                            log(f"WITHDRAW CHECK: weekly halt hit — "
                                f"withdraw any unwithdrawn profits "
                                f"above ${WITHDRAW_BASE:.0f} first")
                    time.sleep(3600)
                    continue
                # evaluate setup
                rg = get_range()
                price = rg["price"]
                chg1h = (rg["last_close"] - rg["prev_close"]) / rg["prev_close"]
                range_w = (rg["high24"] - rg["low24"]) / price
                chg7 = get_weekly_change()
                wk7 = f"{chg7 * 100:+.1f}%" if chg7 is not None else "?"
                if heartbeat % 30 == 0:
                    log(f"watch price={price:.2f} 24h=[{rg['low24']:.2f},"
                        f"{rg['high24']:.2f}] width={range_w*100:.1f}% "
                        f"1h_chg={chg1h*100:+.2f}% 7d={wk7} "
                        f"atr={rg['atr']:.2f} "
                        f"bal=${balance:.2f} pnl_bot=${bot_pnl:.2f} "
                        f"pnl_acct=${acct_pnl_today:.2f}")
                # range too tight to fade: skip (avoid noise trading)
                if range_w < 0.03:
                    time.sleep(CHECK_SEC)
                    continue
                # wild 1h moves (event-driven chop): skip the signal
                if abs(chg1h) > 0.02:
                    if heartbeat % 30 == 0:
                        log(f"SKIP: 1h move {chg1h * 100:+.2f}% too wild")
                    time.sleep(CHECK_SEC)
                    continue
                # weekly change unknown: fail closed
                if chg7 is None:
                    if heartbeat % 30 == 0:
                        log("SKIP: weekly change unknown, fail closed")
                    time.sleep(CHECK_SEC)
                    continue
                # regime (v1.4 #1): chop -> fade the range edges;
                # trend -> only pullback entries WITH the trend
                regime = "trend" if abs(chg7) > REGIME_CHG else "chop"
                # quality gate (v1.5 #6): narrow chop days are watch
                # only — 90% waiting. Reversal trades bypass it.
                if (not reversal and range_w < 0.05
                        and abs(chg7) <= REGIME_CHG):
                    if heartbeat % 30 == 0:
                        log(f"SKIP: no-story day, range {range_w*100:.1f}% "
                            f"7d {chg7*100:+.1f}% — watching only")
                    time.sleep(CHECK_SEC)
                    continue
                # risk per trade: ladder by balance (v1.5 #5), halved
                # again after 2 consecutive losses (v1.4 #5)
                risk_pct = risk_pct_for(balance, state)
                # momentum confirmation (v1.4 #2): this hour's candle
                # must agree with the trade direction
                bull_hour = price > rg["cur_open"]
                bear_hour = price < rg["cur_open"]
                side = None
                if reversal:
                    # confirmed breakout after a failed fade: trade it
                    # as a trend entry (TP 2R, 24h expiry)
                    if reversal == "LONG" and chg1h > -0.01 and bull_hour:
                        side = "LONG"
                    elif reversal == "SHORT" and chg1h < 0.01 and bear_hour:
                        side = "SHORT"
                    regime = "trend"
                elif regime == "chop":
                    if (price >= rg["high24"] * 0.995
                            and chg1h < 0.01 and bear_hour):
                        side = "SHORT"
                    elif (price <= rg["low24"] * 1.005
                            and chg1h > -0.01 and bull_hour):
                        side = "LONG"
                else:
                    if (chg7 > 0 and price <= rg["low24"] * 1.005
                            and chg1h > -0.01 and bull_hour):
                        side = "LONG"
                    elif (chg7 < 0 and price >= rg["high24"] * 0.995
                            and chg1h < 0.01 and bear_hour):
                        side = "SHORT"
                if side is None:
                    time.sleep(CHECK_SEC)
                    continue
                # ATR-adaptive stop (v1.4 #6): distance is the larger of
                # the range-edge distance and 1x ATR(14, 1h). For a
                # reversal trade the edge is the BROKEN one.
                if reversal:
                    if side == "LONG":
                        edge_dist = price - rg["high24"] * 0.988
                        stop = price - max(edge_dist, rg["atr"])
                    else:
                        edge_dist = rg["low24"] * 1.012 - price
                        stop = price + max(edge_dist, rg["atr"])
                elif side == "SHORT":
                    edge_dist = rg["high24"] * 1.012 - price
                    stop = price + max(edge_dist, rg["atr"])
                else:
                    edge_dist = price - rg["low24"] * 0.988
                    stop = price - max(edge_dist, rg["atr"])
                dist = abs(stop - price)
                if dist <= 0:
                    time.sleep(CHECK_SEC)
                    continue
                tp_mult = 1.5 if regime == "chop" else 2.0
                tp = (price - tp_mult * dist if side == "SHORT"
                      else price + tp_mult * dist)
                full_qty = round_down(balance * risk_pct / dist, QTY_STEP)
                if full_qty * price < MIN_NOTIONAL or full_qty <= 0:
                    time.sleep(CHECK_SEC)
                    continue
                # scaled entry (v1.4 #4): open half now, half at +0.5R
                qty = round_down(full_qty / 2, QTY_STEP)
                if qty * price < MIN_NOTIONAL or qty <= 0:
                    qty = full_qty
                # one-sentence trade thesis (v1.5 #2): why, how, exit
                expiry_h = 24 if regime == "trend" else 12
                if reversal:
                    trigger = (f"reversal breakout {side} after "
                               f"{state.get('last_close_side')} stop-out")
                elif regime == "chop":
                    trigger = (f"fade {'high24' if side == 'SHORT' else 'low24'} "
                               f"{rg['high24'] if side == 'SHORT' else rg['low24']:.2f}")
                else:
                    trigger = (f"pullback {'low24' if side == 'LONG' else 'high24'} "
                               f"{rg['low24'] if side == 'LONG' else rg['high24']:.2f}")
                thesis = (f"{regime} {side} {trigger} / exit: stop "
                          f"{stop:.2f} or TP {tp:.2f} or {expiry_h}h expiry")
                log(f"SETUP {side} price={price:.2f} regime={regime} "
                    f"7d={wk7} stop={stop:.2f} tp={tp:.2f} "
                    f"qty={qty:.3f}/{full_qty:.3f} "
                    f"risk=${balance * risk_pct:.2f} atr={rg['atr']:.2f} "
                    f"thesis: {thesis}")
                # v1.1: snapshot account realized PnL at entry so this
                # trade's PnL = close snapshot minus this one
                state["entry_pnl"] = acct_pnl_today
                state["entry_date"] = utc_date_str()
                state["open_stop"] = round(stop, 2)
                state["open_tp"] = round(tp, 2)
                state["open_side"] = side
                state["open_dist"] = round(dist, 2)
                state["open_qty_full"] = full_qty
                state["scale_done"] = False
                state["tp_stage"] = 0
                state["trail_peak_r"] = 0.0
                state["stop_algo_id"] = None
                state["open_ts"] = time.time()
                state["open_regime"] = regime
                state["open_thesis"] = thesis
                state["peak_r"] = 0.0
                state["close_reason"] = None
                avg, stop_ok = open_trade(
                    side, qty, round(stop, 2), round(tp, 2), state)
                if not stop_ok:
                    log("ENTRY ABORTED: stop could not be placed, "
                        "position closed")
                    if get_position()["amt"] == 0:
                        clear_trade_state(state)
                state["trades"] = state.get("trades", 0) + 1
                save_state(state)
            else:
                try:
                    rg_pos = get_range()
                except Exception as e:
                    log(f"price fetch failed: {e}")
                    rg_pos = None
                manage_open_position(pos, orders, algo_orders, state,
                                     rg_pos["price"] if rg_pos else None,
                                     rg_pos)
                if heartbeat % 10 == 0:
                    log(f"holding amt={pos['amt']} entry={pos['entry']} "
                        f"unrealized={pos['unrealized']:.2f}")
            # track close: if we had a position last loop and now flat, log
            # (simplified: detect via state flag)
            if pos["amt"] != 0:
                state["had_position"] = True
                save_state(state)
            elif state.get("had_position"):
                state["had_position"] = False
                state["last_close_ts"] = time.time()
                pnl_now = today_realized_pnl()
                entry_pnl = state.get("entry_pnl")
                # v1.1: attribute only this bot trade's PnL. The snapshot
                # is a same-UTC-day cumulative total, so it is only valid
                # if entry and close fall on the same UTC day; a trade
                # open across midnight (or a restart mid-trade, where the
                # snapshot is missing) is left unattributed rather than
                # misattributed.
                if (entry_pnl is not None
                        and state.get("entry_date") == utc_date_str()):
                    trade_pnl = pnl_now - entry_pnl
                    rollover_bot_pnl(state)
                    state["bot_pnl_today"] = state.get("bot_pnl_today",
                                                       0.0) + trade_pnl
                    # v1.3: count wins/losses (v1.2 never did)
                    # v1.4 #5: consecutive losses + weekly PnL
                    if state.get("weekly_pnl_week") != week_key():
                        state["weekly_pnl_week"] = week_key()
                        state["weekly_pnl"] = 0.0
                    state["weekly_pnl"] = state.get("weekly_pnl",
                                                    0.0) + trade_pnl
                    if trade_pnl > 0:
                        state["wins"] = state.get("wins", 0) + 1
                        state["consec_losses"] = 0
                    elif trade_pnl < 0:
                        state["losses"] = state.get("losses", 0) + 1
                        state["consec_losses"] = state.get(
                            "consec_losses", 0) + 1
                    # v1.5 #7: remember how the last trade ended so a
                    # confirmed breakout after a failed fade can bypass
                    # the cooldown. A losing exchange close is a
                    # stop-out; a winning one is a TP hit.
                    state["last_close_side"] = state.get("open_side")
                    state["last_close_regime"] = state.get("open_regime")
                    state["last_close_loss"] = trade_pnl < 0
                    state["last_close_reason"] = (
                        state.get("close_reason")
                        or ("stop" if trade_pnl < 0 else "tp"))
                    log(f"POSITION CLOSED. bot trade pnl=${trade_pnl:.2f} "
                        f"bot pnl today=${state['bot_pnl_today']:.2f} "
                        f"weekly=${state['weekly_pnl']:.2f} "
                        f"(account realized today=${pnl_now:.2f}) "
                        f"thesis: {state.get('open_thesis')}")
                else:
                    state["last_close_side"] = state.get("open_side")
                    state["last_close_regime"] = state.get("open_regime")
                    state["last_close_loss"] = None
                    state["last_close_reason"] = (
                        state.get("close_reason") or "unknown")
                    log(f"POSITION CLOSED. bot trade pnl unknown "
                        f"(no same-day entry snapshot, e.g. bot restarted "
                        f"mid-trade or trade crossed UTC midnight) "
                        f"-> not counted; account realized "
                        f"today=${pnl_now:.2f} "
                        f"thesis: {state.get('open_thesis')}")
                clear_trade_state(state)
                save_state(state)

            time.sleep(CHECK_SEC)
        except Exception:
            log("LOOP ERROR:\n" + traceback.format_exc())
            time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
