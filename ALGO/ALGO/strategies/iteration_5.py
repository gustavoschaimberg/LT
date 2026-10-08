# ============================================================
# ITERATION 5 (v5)
# TENDER DECISIONS + PURE CROSS-VENUE ARBITRAGE
# ============================================================
#
# RULES
#
#   ARBITRAGE
#     Crossed books after fees -> BUY MARKET on the cheap venue
#     and SELL MARKET on the expensive one, back to back, right
#     away. Nothing is waited for. The only guard: no new
#     arbitrage on the same stock while those two orders are
#     still OPEN in RIT (checked without blocking, with a
#     timeout), so the same crossed book is never traded twice.
#     Blocked while a tender is being unwound.
#
#   TENDER UNWIND
#     The tender lands on one ticker (e.g. CRZY_M). The unwind
#     trades THAT ticker until its position is back to what it
#     was before the tender (zero if we were flat). Nothing is
#     sent to the other venue, so no +X / -X pairs are created.
#     One order in flight per ticker: a new child is only sent
#     once the previous one is TRANSACTED or CANCELLED.
#       LIMIT at the best price for UNWIND_MARKET_AFTER_TICKS
#       ticks, then MARKET sized to the depth near the best
#       price (wider band after UNWIND_MAX_TICKS).
#
#   RESIDUALS
#     With no tender active, any NET exposure on a stock is
#     flattened with MARKET on the venue that holds it (covers
#     an arbitrage leg that was rejected, or a lock lost when
#     the case was paused).
#
#   SAFETY
#     Per-venue cap for arbitrage positions, arbitrage loss
#     limit, direction-reversal detector, drawdown kill switch,
#     RIT gross/net headroom check before accepting a tender.
#
# Portions of this code were developed with assistance from
# OpenAI ChatGPT (2026) and Anthropic Claude (2026).
#
# ============================================================

import time

from api import (
    get_case,
    get_round_number,
    get_tenders,
    get_securities,
    get_order_book,
    get_position,
    get_order,
    get_trader,
    get_limits,
    accept_tender,
    decline_tender,
    submit_limit_order,
    submit_market_order,
    cancel_order,
    cancel_all_orders,
    book_side,
    RateLimitException,
)

import strategies.iteration_2 as engine


# ============================================================
# GENERAL SETTINGS
# ============================================================

DRY_RUN = False

POLL_INTERVAL = 0.05

BOOK_LIMIT = 100


# ============================================================
# ROUND 5 FEES (case: main 0.05; alternative 0.01 / -0.025)
# ============================================================

MAIN_FEE = 0.05

ALTERNATIVE_FEE = 0.01

ALTERNATIVE_REBATE = 0.025


# ============================================================
# TENDER MODEL (iteration_2 engine)
# ============================================================

LAMBDA_INVENTORY = 0.002

LAMBDA_LIQUIDATION = 0.35

MIN_VISIBLE_COVERAGE = 0.25

MIN_EXPECTED_PROFIT = 0.0


# ============================================================
# POSITION LIMITS (fallback when /limits is unavailable)
# ============================================================

NET_POSITION_LIMIT = 100_000

GROSS_POSITION_LIMIT = 200_000

# Never plan to use more than this share of a RIT limit.
LIMIT_USAGE_CAP = 0.95


# ============================================================
# UNWIND
# ============================================================

# Largest child order (also capped by RIT max_trade_size).
MAX_CHILD_SIZE = 5_000

# Ticks holding tender inventory before children go MARKET.
UNWIND_MARKET_AFTER_TICKS = 5

# LIMIT children in a row not fully filled before MARKET.
LIMIT_MISSES_BEFORE_MARKET = 2

# Ticks holding tender inventory before MARKET children may
# walk the wider band.
UNWIND_MAX_TICKS = 15

# MARKET children are sized to the depth within this distance
# of the best price.
MARKET_SLIPPAGE_BAND = 0.05

URGENT_SLIPPAGE_BAND = 0.15

# A LIMIT child still OPEN this long after it could have
# executed (RIT execution delay + this) is cancelled.
LIMIT_CANCEL_AFTER = 0.25

# Any child still OPEN this long (plus delay) is given up on:
# LIMIT cancelled, MARKET assumed done.
CHILD_MAX_WAIT = 2.00

# After a child finishes, give /securities this long to show
# the fill before reading the position for the next child.
POSITION_SETTLE_TIME = 0.10

# Tender inventory must show up within this time.
TENDER_APPEAR_TIMEOUT = 6.00


# ============================================================
# ARBITRAGE (IMMEDIATE MARKET / MARKET)
# ============================================================

ARBITRAGE_ENABLED = True

MIN_ARBITRAGE_NET_EDGE = 0.05

MAX_ARBITRAGE_SIZE = 1_000

# |position| per ticker that arbitrage may build
# (e.g. +20k CRZY_A / -20k CRZY_M at most).
MAX_VENUE_POSITION = 20_000

# The two legs of one arbitrage are considered done when both
# left OPEN in RIT, or after this long (plus delay).
ARB_MAX_WAIT = 2.00

# Arbitrage is switched off for the round when its realized
# PnL (from RIT fills) falls below this.
ARB_MAX_LOSS = 3_000.0


# ============================================================
# SAFETY
# ============================================================

# NLV drop from its peak that halts ALL automated trading for
# the round. None disables the kill switch.
MAX_DRAWDOWN = 40_000.0

# BUY<->SELL reversals allowed on one ticker inside one unwind
# episode. Correct fills never reverse; reversals mean the
# position feed and our orders disagree.
MAX_DIRECTION_FLIPS = 2

HEARTBEAT_SECONDS = 5.0


# ============================================================
# RUNTIME STATE
# ============================================================
#
# ACTIVE_TENDER (created by make_tender_lock, here or in
# iteration_6):
#
# {
#     "tender_id", "base", "tender_action", "tender_quantity",
#     "positions_before": {ticker: position},  <- per-ticker
#     "position_before": net before (for iteration_6 prints),
#     "expected_after": net expected,
#     "ticker": ticker holding the inventory (set when seen),
#     "state": "WAITING_FOR_TENDER" | "UNWINDING",
#     "inventory_tick", "limit_misses", "mode_key"
# }
#
# ============================================================

ACTIVE_TENDER = None

# ticker -> {"order_id", "type", "action", "quantity",
#            "position_before", "sent", "cancel_sent",
#            "poll_failures"}
OPEN_CHILD = {}

# base -> {"buy": order, "sell": order, "sent", "quantity",
#          "buy_ticker", "sell_ticker", "mid"}
ARB_INFLIGHT = {}

HALTED = False

HALT_REASON = None

PEAK_NLV = None

ARB_DISABLED = False

ARB_PNL = 0.0

HALTED_TICKERS = set()

LAST_DIRECTION = {}

DIRECTION_FLIPS = {}

SECURITY_META = {}

LIMITS_CACHE = {"at": 0.0, "groups": None}

LIMITS_CACHE_TTL = 0.25

ACCEPT_FAILURES = {}

LAST_HEARTBEAT = 0.0


# iteration_7 compatibility (it imports these names).
HALTED_BASES = HALTED_TICKERS


# ============================================================
# RESET / CONFIGURE
# ============================================================

def reset_runtime_state():

    global ACTIVE_TENDER, HALTED, HALT_REASON, PEAK_NLV
    global ARB_DISABLED, ARB_PNL, LAST_HEARTBEAT

    ACTIVE_TENDER = None
    HALTED = False
    HALT_REASON = None
    PEAK_NLV = None
    ARB_DISABLED = False
    ARB_PNL = 0.0
    LAST_HEARTBEAT = 0.0

    OPEN_CHILD.clear()
    ARB_INFLIGHT.clear()
    HALTED_TICKERS.clear()
    LAST_DIRECTION.clear()
    DIRECTION_FLIPS.clear()
    SECURITY_META.clear()
    ACCEPT_FAILURES.clear()
    LIMITS_CACHE.update(at=0.0, groups=None)

    # Volatility history must not leak from the previous
    # round (different start prices inflate sigma).
    engine.price_history.clear()
    engine.last_history_tick = None


def configure_round_5_engine():

    engine.MAIN_FEE = MAIN_FEE
    engine.ALTERNATIVE_FEE = ALTERNATIVE_FEE
    engine.ALTERNATIVE_REBATE = ALTERNATIVE_REBATE

    engine.LAMBDA_INVENTORY = LAMBDA_INVENTORY
    engine.LAMBDA_LIQUIDATION = LAMBDA_LIQUIDATION
    engine.MIN_VISIBLE_COVERAGE = MIN_VISIBLE_COVERAGE
    engine.MIN_EXPECTED_PROFIT = MIN_EXPECTED_PROFIT

    engine.NET_POSITION_LIMIT = NET_POSITION_LIMIT
    engine.GROSS_POSITION_LIMIT = GROSS_POSITION_LIMIT


# ============================================================
# BASIC HELPERS
# ============================================================

def remaining_quantity(order):

    quantity = float(order.get("quantity", 0) or 0)
    filled = float(order.get("quantity_filled", 0) or 0)

    return max(quantity - filled, 0)


def get_base_ticker(ticker):

    return str(ticker).split("_")[0]


def get_market_pairs(securities):

    tickers = {
        security.get("ticker")
        for security in securities
        if security.get("ticker")
    }

    pairs = {}

    for ticker in tickers:

        base = get_base_ticker(ticker)
        main = f"{base}_M"
        alternative = f"{base}_A"

        if main in tickers and alternative in tickers:
            pairs[base] = {"main": main, "alternative": alternative}

    return pairs


def ticker_position(ticker, securities):

    for security in securities:

        if security.get("ticker") == ticker:
            return int(security.get("position", 0) or 0)

    return 0


def get_base_position(base, securities):
    """
    NET exposure across every ticker of the stock.
    """

    total = 0

    for security in securities:

        if get_base_ticker(security.get("ticker", "")) == base:
            total += int(security.get("position", 0) or 0)

    return total


def get_live_base_position(base):

    return get_base_position(base, get_securities())


def snapshot_positions(base, securities=None):
    """
    {ticker: position} for every ticker of the stock.
    """

    if securities is None:
        securities = get_securities()

    return {
        security.get("ticker"): int(security.get("position", 0) or 0)
        for security in securities
        if get_base_ticker(security.get("ticker", "")) == base
    }


def synced_securities(securities=None):
    """
    Kept for iteration_7: positions are read straight from RIT.
    """

    return get_securities() if securities is None else securities


def fee_for(ticker):

    return MAIN_FEE if str(ticker).endswith("_M") else ALTERNATIVE_FEE


def get_best_order(book, side):
    """
    Best price level, with ALL quantity resting at that price.
    """

    levels = {}

    for order in book_side(book, side):

        quantity = remaining_quantity(order)

        if quantity <= 0:
            continue

        price = round(float(order["price"]), 4)
        levels[price] = levels.get(price, 0.0) + quantity

    if not levels:
        return None

    price = max(levels) if side == "bids" else min(levels)

    return {"price": price, "quantity": levels[price]}


def depth_within(book, side, best_price, band):
    """
    Quantity resting within `band` of the best price.
    """

    total = 0.0

    for order in book_side(book, side):

        quantity = remaining_quantity(order)

        if quantity <= 0:
            continue

        price = float(order["price"])

        if side == "bids" and price >= best_price - band - 1e-9:
            total += quantity

        elif side == "asks" and price <= best_price + band + 1e-9:
            total += quantity

    return total


# ============================================================
# SECURITY PARAMETERS FROM THE API
# ============================================================

def load_security_meta():

    try:
        securities = get_securities()
    except Exception as error:
        print(f"Could not load security parameters: {error}")
        return

    for security in securities:

        ticker = security.get("ticker")

        if not ticker:
            continue

        try:
            delay = max(float(security.get("execution_delay_ms") or 0), 0.0) / 1000.0
        except (TypeError, ValueError):
            delay = 0.0

        try:
            max_trade = int(security.get("max_trade_size") or 0) or None
        except (TypeError, ValueError):
            max_trade = None

        SECURITY_META[ticker] = {
            "delay": delay,
            "max_trade": max_trade,
            "fee": security.get("trading_fee"),
            "rebate": security.get("limit_order_rebate"),
            "api_orders_per_second": security.get("api_orders_per_second"),
        }


def print_security_meta():

    if not SECURITY_META:
        return

    print("API SECURITY PARAMETERS (compare fees with the case):")

    for ticker in sorted(SECURITY_META):

        meta = SECURITY_META[ticker]

        print(
            f"  {ticker:<8} fee={meta['fee']}  rebate={meta['rebate']}  "
            f"delay={meta['delay'] * 1000:.0f}ms  max_trade={meta['max_trade']}  "
            f"api_orders/s={meta['api_orders_per_second']}"
        )


def order_size_cap(ticker):

    if not SECURITY_META:
        load_security_meta()

    cap = MAX_CHILD_SIZE
    max_trade = SECURITY_META.get(ticker, {}).get("max_trade")

    if max_trade:
        cap = min(cap, max_trade)

    return cap


def execution_delay(ticker):

    return SECURITY_META.get(ticker, {}).get("delay", 0.0)


# ============================================================
# RIT LIMITS
# ============================================================

def get_limit_groups(securities=None):
    """
    RIT's own gross / net usage and limits (cached briefly).
    Falls back to |position| summed per ticker and the module
    constants when /limits is unavailable.
    """

    now = time.time()

    if LIMITS_CACHE["groups"] is not None and now - LIMITS_CACHE["at"] < LIMITS_CACHE_TTL:
        return LIMITS_CACHE["groups"]

    groups = []

    try:
        raw = get_limits()
    except Exception:
        raw = None

    if isinstance(raw, list):

        for limit in raw:

            if not isinstance(limit, dict):
                continue

            try:
                groups.append({
                    "gross": float(limit.get("gross") or 0.0),
                    "net": float(limit.get("net") or 0.0),
                    "gross_limit": float(limit.get("gross_limit") or 0.0),
                    "net_limit": float(limit.get("net_limit") or 0.0),
                })
            except (TypeError, ValueError):
                continue

    if not groups:

        if securities is None:
            securities = get_securities()

        positions = [int(s.get("position", 0) or 0) for s in securities]

        groups.append({
            "gross": float(sum(abs(p) for p in positions)),
            "net": float(sum(positions)),
            "gross_limit": float(GROSS_POSITION_LIMIT),
            "net_limit": float(NET_POSITION_LIMIT),
        })

    LIMITS_CACHE.update(at=now, groups=groups)

    return groups


def invalidate_limits_cache():

    LIMITS_CACHE["at"] = 0.0


def tender_fits_limits(signed_quantity):
    """
    Conservative check before accepting a tender: the tender
    adds |quantity| of gross and signed_quantity of net.
    Returns (ok, reason).
    """

    quantity = abs(signed_quantity)

    for group in get_limit_groups():

        if group["gross_limit"] > 0 and group["gross"] + quantity > LIMIT_USAGE_CAP * group["gross_limit"]:
            return False, (
                f"gross {group['gross']:,.0f} + {quantity:,.0f} would exceed "
                f"{LIMIT_USAGE_CAP:.0%} of the gross limit {group['gross_limit']:,.0f}"
            )

        if group["net_limit"] > 0 and abs(group["net"] + signed_quantity) > LIMIT_USAGE_CAP * group["net_limit"]:
            return False, (
                f"net {group['net']:+,.0f} {signed_quantity:+,.0f} would exceed "
                f"{LIMIT_USAGE_CAP:.0%} of the net limit {group['net_limit']:,.0f}"
            )

    return True, ""


def format_limits():

    parts = [
        f"gross {g['gross']:,.0f}/{g['gross_limit']:,.0f} net {g['net']:+,.0f}/{g['net_limit']:,.0f}"
        for g in get_limit_groups()
    ]

    return " | ".join(parts) if parts else "limits n/a"


# ============================================================
# ORDER STATUS (non-blocking helpers)
# ============================================================

def order_is_open(order):

    return str((order or {}).get("status", "")).upper() == "OPEN"


def refresh_order(order):
    """
    Returns the latest version of `order` from RIT, or None if
    RIT would not return it.
    """

    try:
        latest = get_order(order["order_id"])
    except Exception:
        return None

    if isinstance(latest, list):
        latest = latest[0] if latest else None

    if isinstance(latest, dict) and latest.get("order_id") is not None:
        return latest

    return None


def order_fill(order):
    """
    (filled_quantity, vwap) from an order object.
    """

    filled = int(round(float(order.get("quantity_filled") or 0)))
    vwap = order.get("vwap")

    return filled, (float(vwap) if vwap is not None else None)


# ============================================================
# ONE CHILD ORDER IN FLIGHT PER TICKER
# ============================================================

# send_child removed: no non-arbitrage order path.

def child_in_flight(ticker):
    """
    True while the last child on `ticker` is still OPEN.

    When it has finished (TRANSACTED / CANCELLED), logs the
    fill, clears it and returns False. LIMIT children still
    OPEN after the execution delay are cancelled; anything
    still OPEN after CHILD_MAX_WAIT is given up on.
    """

    child = OPEN_CHILD.get(ticker)

    if child is None:
        return False

    order = child["order"]
    age = time.time() - child["sent"]
    delay = execution_delay(ticker)

    if order.get("order_id") is not None:

        latest = refresh_order(order)

        if latest is not None:
            child["order"] = order = latest
            child["poll_failures"] = 0
        else:
            child["poll_failures"] += 1

    if order_is_open(order) and child["poll_failures"] < 5:

        if child["type"] == "LIMIT" and not child["cancel_sent"] and age >= delay + LIMIT_CANCEL_AFTER:

            try:
                cancel_order(order["order_id"])
            except Exception:
                pass  # may have filled meanwhile

            child["cancel_sent"] = True

        if age < delay + CHILD_MAX_WAIT:
            return True

        # Given up: cancel a LIMIT remainder, assume MARKET done.
        if child["type"] == "LIMIT" and order.get("order_id") is not None:
            try:
                cancel_order(order["order_id"])
            except Exception:
                pass

        print(f"CHILD UNCONFIRMED | {ticker} | order {order.get('order_id')} still OPEN after {age:.1f}s")

    filled, vwap = order_fill(order)

    if filled:
        print(
            f"  filled {filled:,} {ticker}"
            + (f" @ {vwap:.4f}" if vwap is not None else "")
        )

    del OPEN_CHILD[ticker]
    invalidate_limits_cache()

    time.sleep(POSITION_SETTLE_TIME)

    return False


def cancel_children(ticker):

    OPEN_CHILD.pop(ticker, None)

    try:
        cancel_all_orders(ticker)
    except Exception:
        pass


# ============================================================
# BLOCKING EXECUTION (used by iteration_7 / iteration_8)
# ============================================================

# submit_order removed: no non-arbitrage order path.

def resolve_orders(handles):
    """
    Blocks until each handle's child has finished. Returns
    [(filled, vwap), ...] aligned with `handles`.
    """

    results = []

    for handle in handles:

        if handle is None:
            results.append((0, None))
            continue

        ticker = handle["ticker"]
        child = OPEN_CHILD.get(ticker)

        while child_in_flight(ticker):
            time.sleep(0.02)

        if child is None:
            results.append((0, None))
            continue

        results.append(order_fill(child["order"]))

    return results


# execute_ioc removed: no non-arbitrage order path.

def register_direction(ticker, action):
    """
    Returns False (and halts the ticker) when unwind orders
    keep reversing direction inside one episode.
    """

    last = LAST_DIRECTION.get(ticker)

    if last is not None and last != action:

        DIRECTION_FLIPS[ticker] = DIRECTION_FLIPS.get(ticker, 0) + 1

        print(f"DIRECTION FLIP | {ticker} | {last} -> {action} | flips {DIRECTION_FLIPS[ticker]}")

        if DIRECTION_FLIPS[ticker] > MAX_DIRECTION_FLIPS:

            HALTED_TICKERS.add(ticker)
            cancel_children(ticker)

            print("\n" + "!" * 70)
            print(f"{ticker} HALTED: unwind kept reversing direction.")
            print("The position feed and the fills disagree.")
            print(f"Unwind {ticker} MANUALLY for the rest of the round.")
            print("!" * 70)

            return False

    LAST_DIRECTION[ticker] = action

    return True


def end_episode(ticker):

    LAST_DIRECTION.pop(ticker, None)
    DIRECTION_FLIPS[ticker] = 0


# ============================================================
# KILL SWITCH
# ============================================================

def check_kill_switch():
    """
    Halts all automated trading for the round when NLV falls
    MAX_DRAWDOWN below its peak. Returns True when halted.
    """

    global HALTED, HALT_REASON, PEAK_NLV

    if HALTED:
        return True

    if MAX_DRAWDOWN is None:
        return False

    try:
        nlv = float(get_trader().get("nlv") or 0.0)
    except Exception:
        return False

    if PEAK_NLV is None or nlv > PEAK_NLV:
        PEAK_NLV = nlv

    drawdown = PEAK_NLV - nlv

    if drawdown <= MAX_DRAWDOWN:
        return False

    HALTED = True
    HALT_REASON = f"NLV {nlv:,.0f} is {drawdown:,.0f} below its peak {PEAK_NLV:,.0f}"

    try:
        cancel_all_orders()
    except Exception:
        pass

    OPEN_CHILD.clear()

    print("\n" + "!" * 70)
    print("KILL SWITCH: automated trading stopped for this round.")
    print(HALT_REASON)
    print("Positions are NOT flattened automatically.")
    print("Check the portfolio and decide manually.")
    print("!" * 70)

    return True


# ============================================================
# TENDER LOCK
# ============================================================

# make_tender_lock removed: no tender unwind or inventory flattening.

# detect_inventory_ticker removed: no tender unwind or inventory flattening.

# process_active_tender removed: no tender unwind or inventory flattening.

# send_unwind_child removed: no tender unwind or inventory flattening.

def process_tenders(processed_tenders, securities, current_tick):
    """Evaluate fixed tenders only. Never send an unwind order."""
    if HALTED:
        return
    for tender in get_tenders():
        tender_id = tender["tender_id"]
        if tender_id in processed_tenders:
            continue
        if tender.get("is_fixed_bid", True) is False:
            continue
        try:
            result = engine.evaluate_tender(tender, securities)
            engine.print_tender_result(result)
            action = str(tender.get("action", "")).upper()
            quantity = int(tender.get("quantity", 0))
            accept = result.get("decision") == "ACCEPT" and action in ("BUY", "SELL") and quantity > 0
            if accept:
                invalidate_limits_cache()
                fits, reason = tender_fits_limits(quantity if action == "BUY" else -quantity)
                if not fits:
                    print(f"TENDER {tender_id}: declined (limits: {reason})")
                    accept = False
            if DRY_RUN:
                print(f"[DRY RUN] TENDER {tender_id}: {'ACCEPT' if accept else 'DECLINE'}")
            elif accept:
                accept_tender(tender_id)
                invalidate_limits_cache()
                print(f"TENDER {tender_id}: ACCEPTED. Inventory will NOT be unwound.")
            else:
                decline_tender(tender_id)
                print(f"TENDER {tender_id}: DECLINED.")
            processed_tenders.add(tender_id)
            return
        except Exception as error:
            ACCEPT_FAILURES[tender_id] = ACCEPT_FAILURES.get(tender_id, 0) + 1
            print(f"Tender {tender_id} error: {error}")
            if ACCEPT_FAILURES[tender_id] >= 2:
                processed_tenders.add(tender_id)
            return

# flatten_residuals removed: no tender unwind or inventory flattening.

def build_opportunity(buy_ticker, ask, sell_ticker, bid):

    net_edge = (bid["price"] - fee_for(sell_ticker)) - (ask["price"] + fee_for(buy_ticker))
    quantity = int(min(ask["quantity"], bid["quantity"], MAX_ARBITRAGE_SIZE))

    if quantity <= 0:
        return None

    return {
        "base": get_base_ticker(buy_ticker),
        "buy_ticker": buy_ticker, "buy_price": ask["price"],
        "sell_ticker": sell_ticker, "sell_price": bid["price"],
        "quantity": quantity, "net_edge": net_edge,
    }


def find_arbitrage(main_ticker, alternative_ticker):

    main_book = get_order_book(main_ticker, limit=5)
    alt_book = get_order_book(alternative_ticker, limit=5)

    main_bid, main_ask = get_best_order(main_book, "bids"), get_best_order(main_book, "asks")
    alt_bid, alt_ask = get_best_order(alt_book, "bids"), get_best_order(alt_book, "asks")

    options = []

    if alt_ask and main_bid:
        options.append(build_opportunity(alternative_ticker, alt_ask, main_ticker, main_bid))

    if main_ask and alt_bid:
        options.append(build_opportunity(main_ticker, main_ask, alternative_ticker, alt_bid))

    options = [o for o in options if o is not None and o["net_edge"] >= MIN_ARBITRAGE_NET_EDGE]

    if not options:
        return None

    return max(options, key=lambda o: o["net_edge"])


def arbitrage_size_allowed(opportunity, securities):
    """
    Caps the size by the per-venue position cap and by RIT's
    gross headroom (an arbitrage adds up to 2 x quantity).
    """

    quantity = int(opportunity["quantity"])

    buy_position = ticker_position(opportunity["buy_ticker"], securities)
    sell_position = ticker_position(opportunity["sell_ticker"], securities)

    quantity = min(quantity, MAX_VENUE_POSITION - buy_position, MAX_VENUE_POSITION + sell_position)

    if quantity <= 0:
        return 0

    for group in get_limit_groups(securities):

        if group["gross_limit"] > 0:
            headroom = LIMIT_USAGE_CAP * group["gross_limit"] - group["gross"]
            quantity = min(quantity, int(headroom // 2))

    return max(int(quantity), 0)


def fire_arbitrage(opportunity, quantity):
    """Send only BUY on cheap venue and SELL on expensive venue.

    A rejected second leg causes a warning and blocks further trades in
    this base until manually reviewed. No cleanup order is submitted.
    """
    base = opportunity["base"]
    buy_ticker, sell_ticker = opportunity["buy_ticker"], opportunity["sell_ticker"]
    print(f"ARBITRAGE {base}: BUY {quantity} {buy_ticker} / SELL {quantity} {sell_ticker} | edge {opportunity['net_edge']:.4f}")
    if DRY_RUN:
        return
    try:
        buy = submit_market_order(buy_ticker, "BUY", quantity)
    except Exception as error:
        print(f"ARBITRAGE BUY failed: {error}")
        return
    try:
        sell = submit_market_order(sell_ticker, "SELL", quantity)
    except Exception as error:
        print(f"CRITICAL: SELL leg failed: {error}. No hedge/unwind sent. {base} halted.")
        HALTED_TICKERS.update((buy_ticker, sell_ticker))
        ARB_INFLIGHT[base] = {
            "buy": buy if isinstance(buy, dict) else {"order_id": None, "status": "TRANSACTED"},
            "sell": {"order_id": None, "status": "CANCELLED"},
            "buy_ticker": buy_ticker, "sell_ticker": sell_ticker,
            "quantity": quantity, "sent": time.time(),
            "mid": (opportunity["buy_price"] + opportunity["sell_price"]) / 2,
        }
        return
    ARB_INFLIGHT[base] = {
        "buy": buy if isinstance(buy, dict) else {"order_id": None, "status": "TRANSACTED"},
        "sell": sell if isinstance(sell, dict) else {"order_id": None, "status": "TRANSACTED"},
        "buy_ticker": buy_ticker, "sell_ticker": sell_ticker,
        "quantity": quantity, "sent": time.time(),
        "mid": (opportunity["buy_price"] + opportunity["sell_price"]) / 2,
    }
    invalidate_limits_cache()

def settle_arbitrage(base):
    """
    Non-blocking: refreshes the two legs; when both are done
    (or timed out) books the realized PnL. Returns True while
    still in flight.
    """

    global ARB_PNL, ARB_DISABLED

    arb = ARB_INFLIGHT.get(base)

    if arb is None:
        return False

    age = time.time() - arb["sent"]
    delay = max(execution_delay(arb["buy_ticker"]), execution_delay(arb["sell_ticker"]))

    pending = False

    for leg in ("buy", "sell"):

        order = arb[leg]

        if order.get("order_id") is None or not order_is_open(order):
            continue

        latest = refresh_order(order)

        if latest is not None:
            arb[leg] = order = latest

        if order_is_open(order):
            pending = True

    if pending and age < delay + ARB_MAX_WAIT:
        return True

    bought, buy_vwap = order_fill(arb["buy"])
    sold, sell_vwap = order_fill(arb["sell"])

    cash = 0.0

    if bought:
        cash -= bought * ((buy_vwap if buy_vwap is not None else arb["mid"]) + fee_for(arb["buy_ticker"]))

    if sold:
        cash += sold * ((sell_vwap if sell_vwap is not None else arb["mid"]) - fee_for(arb["sell_ticker"]))

    imbalance = bought - sold
    pnl = cash + imbalance * arb["mid"]

    ARB_PNL += pnl

    print(
        f"  arb done | bought {bought:,} @ {buy_vwap or 0:.4f} | sold {sold:,} @ {sell_vwap or 0:.4f} | "
        f"pnl ${pnl:,.2f} | total ${ARB_PNL:,.2f}"
        + (f" | UNHEDGED {imbalance:+,} (MANUAL REVIEW REQUIRED)" if imbalance else "")
    )

    del ARB_INFLIGHT[base]
    invalidate_limits_cache()

    if ARB_PNL <= -ARB_MAX_LOSS and not ARB_DISABLED:

        ARB_DISABLED = True

        print("\n" + "!" * 70)
        print(f"ARBITRAGE DISABLED FOR THIS ROUND | realized ${ARB_PNL:,.2f}")
        print("!" * 70)

    return False


def process_arbitrage(securities):
    """Only execute genuine simultaneous cross-venue arbitrage."""
    if HALTED:
        return
    securities = get_securities()
    pairs = get_market_pairs(securities)
    for base in list(ARB_INFLIGHT):
        settle_arbitrage(base)
    if not ARBITRAGE_ENABLED or ARB_DISABLED:
        return
    for base, pair in pairs.items():
        if base in ARB_INFLIGHT:
            continue
        if pair["main"] in HALTED_TICKERS or pair["alternative"] in HALTED_TICKERS:
            continue
        try:
            opportunity = find_arbitrage(pair["main"], pair["alternative"])
            if opportunity is None:
                continue
            quantity = arbitrage_size_allowed(opportunity, securities)
            if quantity > 0:
                fire_arbitrage(opportunity, quantity)
        except Exception as error:
            print(f"Arbitrage error {base}: {error}")

def heartbeat(current_tick, securities):

    global LAST_HEARTBEAT

    now = time.time()

    if now - LAST_HEARTBEAT < HEARTBEAT_SECONDS:
        return

    LAST_HEARTBEAT = now

    if False and ACTIVE_TENDER is not None:
        state = (
            f"TENDER {ACTIVE_TENDER['tender_id']} {ACTIVE_TENDER.get('state', '?')}"
            + (f" on {ACTIVE_TENDER['ticker']}" if ACTIVE_TENDER.get("ticker") else "")
        )
    elif HALTED:
        state = "HALTED"
    else:
        state = "IDLE" + (" (arb off)" if ARB_DISABLED or not ARBITRAGE_ENABLED else "")

    positions = " ".join(
        f"{s.get('ticker')}:{int(s.get('position', 0) or 0):+,}"
        for s in sorted(securities, key=lambda s: str(s.get("ticker")))
        if s.get("ticker")
    )

    print(f"[tick {current_tick}] {state} | {positions} | {format_limits()} | arb pnl ${ARB_PNL:,.0f}")


# ============================================================
# ITERATION 5 MAIN LOOP
# ============================================================

def run_iteration_5(config):
    reset_runtime_state()
    configure_round_5_engine()
    load_security_meta()
    print("ITERATION 5 | TENDER ACCEPT/DECLINE + PURE ARBITRAGE ONLY")
    print("No tender unwind, no residual flattening, no automatic hedging.")
    print_security_meta()
    processed_tenders = set()
    while True:
        try:
            case_info = get_case()
            if get_round_number(case_info) != config["iteration"]:
                print(f"Iteration {config['iteration']} ended.")
                return
            if case_info.get("status") != "ACTIVE":
                return
            current_tick = int(case_info.get("tick", 0))
            securities = get_securities()
            engine.update_market_history(securities, current_tick)
            heartbeat(current_tick, securities)
            if check_kill_switch():
                time.sleep(0.25)
                continue
            process_tenders(processed_tenders, securities, current_tick)
            process_arbitrage(securities)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("Iteration 5 stopped by user.")
            return
        except Exception as error:
            print(f"Iteration 5 error: {error}")
            time.sleep(POLL_INTERVAL)

