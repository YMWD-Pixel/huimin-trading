#!/usr/bin/env python3
"""Huimin trading bot v1.2 — Binance USDT-margined futures, ETHUSDT only.
Style: 熬鹰 (see trading-style-aoying.md):
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
                "had_position": False}


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


def get_range():
    kl = public("/fapi/v1/klines",
                {"symbol": SYMBOL, "interval": "1h", "limit": 24})
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    return {"high24": max(highs), "low24": min(lows),
            "last_close": closes[-1], "prev_close": closes[-2],
            "price": closes[-1]}


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


def open_trade(side, qty, stop_price, tp_price):
    """side: LONG or SHORT. Places market entry + stop + TP."""
    entry_side = "BUY" if side == "LONG" else "SELL"
    exit_side = "SELL" if side == "LONG" else "BUY"
    o = place_order(symbol=SYMBOL, side=entry_side, type="MARKET",
                    quantity=f"{qty:.3f}", **_ps(side))
    log(f"ENTRY {side} qty={qty:.3f} orderId={o.get('orderId')} "
        f"status={o.get('status')}")
    fills = o.get("fills", [])
    avg = (sum(float(f["price"]) * float(f["qty"]) for f in fills)
           / sum(float(f["qty"]) for f in fills)) if fills else None
    log(f"  entry avg price: {avg}")
    # stop-loss: closePosition STOP_MARKET on mark price
    try:
        s = place_order(symbol=SYMBOL, side=exit_side, type="STOP_MARKET",
                        stopPrice=f"{stop_price:.2f}", closePosition="true",
                        workingType="MARK_PRICE", timeInForce="GTC",
                        **_ps(side))
        log(f"  STOP placed orderId={s.get('orderId')} stop={stop_price:.2f}")
    except Exception as e:
        log(f"  STOP PLACEMENT FAILED: {e} (loop will re-place it)")
    # take-profit: reduceOnly limit
    t = place_order(symbol=SYMBOL, side=exit_side, type="LIMIT",
                    price=f"{tp_price:.2f}", quantity=f"{qty:.3f}",
                    timeInForce="GTC", reduceOnly="true", **_ps(side))
    log(f"  TP placed orderId={t.get('orderId')} tp={tp_price:.2f}")
    return avg


def main():
    global HEDGE
    acct = signed("/fapi/v2/account")
    log(f"bot v1.2 start | canTrade={acct.get('canTrade')} "
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

            pos = get_position()
            orders = get_open_orders()

            if pos["amt"] == 0:
                # flat: cancel any leftover stop/TP orders
                if orders:
                    cancel_all()
                # cooldown after a close
                if time.time() - state.get("last_close_ts", 0) < COOLDOWN_SEC:
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
                # evaluate setup
                rg = get_range()
                price = rg["price"]
                chg1h = (rg["last_close"] - rg["prev_close"]) / rg["prev_close"]
                range_w = (rg["high24"] - rg["low24"]) / price
                if heartbeat % 30 == 0:
                    log(f"watch price={price:.2f} 24h=[{rg['low24']:.2f},"
                        f"{rg['high24']:.2f}] width={range_w*100:.1f}% "
                        f"1h_chg={chg1h*100:+.2f}% "
                        f"bal=${balance:.2f} pnl_bot=${bot_pnl:.2f} "
                        f"pnl_acct=${acct_pnl_today:.2f}")
                # range too tight to fade: skip (avoid noise trading)
                if range_w < 0.03:
                    time.sleep(CHECK_SEC)
                    continue
                # SHORT: near 24h high, not surging up
                if price >= rg["high24"] * 0.995 and chg1h < 0.01:
                    stop = rg["high24"] * 1.012
                    dist = stop - price
                    qty = round_down(balance * RISK_PCT / dist, QTY_STEP)
                    tp = price - 1.5 * dist
                    if qty * price >= MIN_NOTIONAL and qty > 0:
                        log(f"SETUP SHORT price={price:.2f} high24="
                            f"{rg['high24']:.2f} stop={stop:.2f} "
                            f"tp={tp:.2f} qty={qty:.3f} risk="
                            f"${balance*RISK_PCT:.2f}")
                        # v1.1: snapshot account realized PnL at entry so
                        # this trade's PnL = close snapshot minus this one
                        state["entry_pnl"] = acct_pnl_today
                        state["entry_date"] = utc_date_str()
                        open_trade("SHORT", qty,
                                   round(stop, 2), round(tp, 2))
                        state["trades"] = state.get("trades", 0) + 1
                        save_state(state)
                # LONG: near 24h low, not plunging
                elif price <= rg["low24"] * 1.005 and chg1h > -0.01:
                    stop = rg["low24"] * 0.988
                    dist = price - stop
                    qty = round_down(balance * RISK_PCT / dist, QTY_STEP)
                    tp = price + 1.5 * dist
                    if qty * price >= MIN_NOTIONAL and qty > 0:
                        log(f"SETUP LONG price={price:.2f} low24="
                            f"{rg['low24']:.2f} stop={stop:.2f} "
                            f"tp={tp:.2f} qty={qty:.3f} risk="
                            f"${balance*RISK_PCT:.2f}")
                        # v1.1: snapshot account realized PnL at entry (see
                        # SHORT branch) so only this trade's PnL counts
                        state["entry_pnl"] = acct_pnl_today
                        state["entry_date"] = utc_date_str()
                        open_trade("LONG", qty,
                                   round(stop, 2), round(tp, 2))
                        state["trades"] = state.get("trades", 0) + 1
                        save_state(state)
            else:
                # position open: make sure a stop order exists
                has_stop = any(o.get("type") == "STOP_MARKET"
                               for o in orders)
                if not has_stop:
                    log(f"WARNING: open position {pos['amt']} but NO stop "
                        f"order! entry={pos['entry']} unrealized="
                        f"{pos['unrealized']:.2f}")
                    # re-place a stop 2% beyond entry against the position
                    side = "SELL" if pos["amt"] > 0 else "BUY"
                    stop = (pos["entry"] * 0.98 if pos["amt"] > 0
                            else pos["entry"] * 1.02)
                    try:
                        place_order(symbol=SYMBOL, side=side,
                                    type="STOP_MARKET",
                                    stopPrice=f"{stop:.2f}",
                                    closePosition="true",
                                    workingType="MARK_PRICE",
                                    timeInForce="GTC",
                                    **_ps(pos["side"]))
                        log(f"  emergency stop placed at {stop:.2f}")
                    except Exception as e:
                        log(f"  emergency stop FAILED: {e}")
                # detect close: position was open, now check realized pnl
                if heartbeat % 10 == 0:
                    log(f"holding amt={pos['amt']} entry={pos['entry']} "
                        f"unrealized={pos['unrealized']:.2f}")
                # if position closed externally, record it next loop via amt==0;
                # mark close time when we see amt return to 0 (handled below)
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
                    log(f"POSITION CLOSED. bot trade pnl=${trade_pnl:.2f} "
                        f"bot pnl today=${state['bot_pnl_today']:.2f} "
                        f"(account realized today=${pnl_now:.2f})")
                else:
                    log(f"POSITION CLOSED. bot trade pnl unknown "
                        f"(no same-day entry snapshot, e.g. bot restarted "
                        f"mid-trade or trade crossed UTC midnight) "
                        f"-> not counted; account realized "
                        f"today=${pnl_now:.2f}")
                state["entry_pnl"] = None
                state["entry_date"] = None
                save_state(state)

            time.sleep(CHECK_SEC)
        except Exception:
            log("LOOP ERROR:\n" + traceback.format_exc())
            time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
