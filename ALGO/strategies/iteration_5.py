# ============================================================
# ITERATION 5 (v3)
# TENDER-LINKED UNWIND + CROSS-VENUE ARBITRAGE
# ============================================================
#
# EXECUTION POLICY
#
#   Arbitrage: MARKET / MARKET
#     - both legs sent together, sized to the top of book
#     - both resolved and positions synced before anything
#       else happens (no re-firing on a book that still shows
#       the edge we already traded)
#     - any difference between the two fills is hedged at
#       once with MARKET
#
#   Tender unwind: LIMIT first, MARKET when exposure lasts
#     - starts with marketable LIMIT at the top of book
#     - switches to MARKET after UNWIND_MARKET_AFTER_TICKS
#       ticks holding the inventory, or after
#       IOC_MISSES_BEFORE_MARKET LIMIT children in a row that
#       did not fully fill
#     - MARKET children are sized to the depth within
#       MARKET_SLIPPAGE_BAND of the best price; after
#       UNWIND_MAX_TICKS the band widens to URGENT_SLIPPAGE_BAND
#     - each step sends one child per venue when both venues
#       price within SECOND_VENUE_TOLERANCE of each other
#
#   Residual NET exposure (broken arbitrage leg, lock lost on
#   PAUSE, late auction fill): flattened with MARKET when idle.
#
# WHY (v1 bug that bled PnL in rounds 5 and 6)
#
#   1. Arbitrage never tracked the orders it had sent. With
#      an execution delay, the next loop (~0.15 s later) still
#      saw the same crossed book and fired again.
#   2. A failed 2nd leg left the 1st leg open as directional
#      exposure, and v1 never flattened non-tender inventory.
#   3. Nothing stopped it: the limit check nets _M and _A, no
#      loss limit, no exposure cap per venue.
#
# SAFETY
#
#   Per-ticker exposure cap, arbitrage loss limit, ping-pong
#   detector, position sync when the API lags, drawdown kill
#   switch.
#
# The interface used by iteration_6 is unchanged.
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
# POSITION LIMITS
# ============================================================

NET_POSITION_LIMIT = 100_000

GROSS_POSITION_LIMIT = 200_000


# ============================================================
# EXECUTION
# ============================================================

# Largest single child order (also capped by max_trade_size).
MAX_CHILD_SIZE = 5_000

# LIMIT children: cancel the unfilled remainder this long
# after the order could have executed (delay + this).
IOC_CANCEL_AFTER = 0.25

# Stop tracking an order after this long (plus delay) and
# fall back to reading the position.
ORDER_MAX_WAIT = 2.00

# How long to wait for /securities to show a fill. After
# that, decisions use the position our fills imply until the
# API catches up (or PENDING_SYNC_MAX_AGE passes).
POSITION_SYNC_TIMEOUT = 1.00

PENDING_SYNC_MAX_AGE = 3.00

# Fixed tender inventory must appear within this time.
TENDER_APPEAR_TIMEOUT = 6.00


# ============================================================
# UNWIND: LIMIT FIRST, MARKET WHEN EXPOSURE LASTS
# ============================================================
#
# The case suggests unwinding in 10-20 ticks.
#
# ============================================================

# Ticks holding tender inventory before children go MARKET.
UNWIND_MARKET_AFTER_TICKS = 5

# LIMIT children in a row that did not fully fill before
# switching to MARKET.
IOC_MISSES_BEFORE_MARKET = 2

# Ticks holding tender inventory before the MARKET children
# may walk the wider (urgent) band.
UNWIND_MAX_TICKS = 15

# A MARKET child is sized to the depth resting within this
# distance of the best price, so it cannot walk further if the
# book does not move before it executes.
MARKET_SLIPPAGE_BAND = 0.05

URGENT_SLIPPAGE_BAND = 0.15

# Also send a child to the second venue when its fee-adjusted
# top price is within this of the best venue.
SECOND_VENUE_TOLERANCE = 0.03


# ============================================================
# ARBITRAGE (MARKET / MARKET)
# ============================================================

ARBITRAGE_ENABLED = True

MIN_ARBITRAGE_NET_EDGE = 0.05

MAX_ARBITRAGE_SIZE = 1_000

ARBITRAGE_COOLDOWN = 0.10

# |position| per ticker that arbitrage may build
# (e.g. +20k CRZY_A / -20k CRZY_M at most).
MAX_VENUE_POSITION = 20_000

# MARKET attempts to hedge a fill difference before leaving
# it to the residual flattener.
ARB_HEDGE_ATTEMPTS = 5

# Arbitrage is switched off for the rest of the round when
# its cumulative PnL falls below -ARB_MAX_LOSS, or after this
# many losing arbitrages in a row.
ARB_MAX_LOSS = 2_000.0

ARB_MAX_CONSECUTIVE_LOSSES = 3


# ============================================================
# SAFETY
# ============================================================

# NLV drop from its peak that halts ALL automated trading
# for the round. None disables the kill switch.
MAX_DRAWDOWN = 40_000.0

# BUY<->SELL reversals allowed inside one unwind episode.
# Correct fills never reverse; reversals mean the position
# feed and our orders disagree, so we stop that security.
MAX_DIRECTION_FLIPS = 2


# ============================================================
# RUNTIME STATE
# ============================================================
#
# ACTIVE_TENDER (created here or by iteration_6):
#
# {
#     "tender_id", "base", "tender_action", "tender_quantity",
#     "position_before",   <- unwind target (net, _M + _A)
#     "expected_after",
#     "accepted_tick",
#     "state": "WAITING_FOR_TENDER" | "UNWINDING",
#     ...
# }
#
# ============================================================

ACTIVE_TENDER = None

HALTED = False

HALT_REASON = None

PEAK_NLV = None

ARB_DISABLED = False

ARB_PNL = 0.0

ARB_CONSECUTIVE_LOSSES = 0

HALTED_BASES = set()

LAST_DIRECTION = {}

DIRECTION_FLIPS = {}

SECURITY_META = {}

# ticker -> {"expected": position, "since": time} while the
# API still shows a position from before our last fill.
PENDING_SYNC = {}


# ============================================================
# RESET / CONFIGURE
# ============================================================

def reset_runtime_state():

    global ACTIVE_TENDER, HALTED, HALT_REASON, PEAK_NLV
    global ARB_DISABLED, ARB_PNL, ARB_CONSECUTIVE_LOSSES

    ACTIVE_TENDER = None
    HALTED = False
    HALT_REASON = None
    PEAK_NLV = None
    ARB_DISABLED = False
    ARB_PNL = 0.0
    ARB_CONSECUTIVE_LOSSES = 0

    HALTED_BASES.clear()
    LAST_DIRECTION.clear()
    DIRECTION_FLIPS.clear()
    SECURITY_META.clear()
    PENDING_SYNC.clear()

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


def get_base_position(base, securities):
    """
    NET exposure across Main + Alternative. Venue positions
    are never flattened individually.
    """

    total = 0

    for security in securities:

        if get_base_ticker(security.get("ticker", "")) == base:
            total += int(security.get("position", 0) or 0)

    return total


def synced_securities(securities=None):
    """
    /securities with our expected positions applied where the
    API has not caught up with our latest fills yet.
    """

    if securities is None:
        securities = get_securities()

    if not PENDING_SYNC:
        return securities

    now = time.time()
    result = []

    for security in securities:

        ticker = security.get("ticker")
        pending = PENDING_SYNC.get(ticker)

        if pending is None:
            result.append(security)
            continue

        live = int(security.get("position", 0) or 0)

        if live == pending["expected"] or now - pending["since"] > PENDING_SYNC_MAX_AGE:
            PENDING_SYNC.pop(ticker, None)
            result.append(security)
            continue

        patched = dict(security)
        patched["position"] = pending["expected"]
        result.append(patched)

    return result


def get_live_base_position(base):

    return get_base_position(base, synced_securities())


def ticker_position(ticker, securities):

    for security in securities:

        if security.get("ticker") == ticker:
            return int(security.get("position", 0) or 0)

    return 0


def effective_position(ticker):

    return ticker_position(ticker, synced_securities(get_securities(ticker)))


def fee_for(ticker):
    """
    Taker fee per share (every order here removes liquidity).
    """

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

        delay_ms = security.get("execution_delay_ms") or 0
        max_trade = security.get("max_trade_size") or 0

        try:
            delay = max(float(delay_ms), 0.0) / 1000.0
        except (TypeError, ValueError):
            delay = 0.0

        try:
            max_trade = int(max_trade) if max_trade else None
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
            f"  {ticker:<8} "
            f"fee={meta['fee']}  "
            f"rebate={meta['rebate']}  "
            f"delay={meta['delay'] * 1000:.0f}ms  "
            f"max_trade={meta['max_trade']}  "
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


# ============================================================
# ORDER EXECUTION
# ============================================================

def wait_for_position(ticker, expected):
    """
    Waits until /securities shows the expected position.

    If it does not within POSITION_SYNC_TIMEOUT, records the
    expectation in PENDING_SYNC so that later decisions use
    the position our fills imply, not the stale one.
    """

    deadline = time.time() + POSITION_SYNC_TIMEOUT

    while True:

        live = get_position(ticker)

        if live == expected:
            PENDING_SYNC.pop(ticker, None)
            return

        if time.time() >= deadline:

            PENDING_SYNC[ticker] = {"expected": expected, "since": time.time()}

            print(
                f"POSITION SYNC | {ticker} | API shows {live:+,} | "
                f"using expected {expected:+,} until it catches up"
            )

            return

        time.sleep(0.02)


def submit_order(ticker, action, quantity, price=None, order_type="LIMIT"):
    """
    Sends one order and returns a handle for resolve_orders(),
    or None if nothing was sent.

    LIMIT : marketable LIMIT at `price`; the remainder is
            cancelled after the execution delay (IOC).
    MARKET: plain MARKET; never cancelled by us.
    """

    action = action.upper()
    order_type = order_type.upper()
    quantity = int(min(quantity, order_size_cap(ticker)))

    if quantity <= 0:
        return None

    if DRY_RUN:
        where = f" @ {price:.2f}" if order_type == "LIMIT" else ""
        print(f"DRY RUN | {order_type} {action} {quantity:,} {ticker}{where}")
        return None

    position_before = effective_position(ticker)

    try:

        if order_type == "MARKET":
            order = submit_market_order(ticker, action, quantity)
        else:
            order = submit_limit_order(ticker, action, quantity, price)

    except RateLimitException as error:
        print(f"RATE LIMIT | {ticker} | wait {error.wait:.2f}s")
        time.sleep(min(error.wait, 1.0))
        return None

    except Exception as error:
        print(f"ORDER ERROR | {order_type} {action} {quantity:,} {ticker} | {error}")
        return None

    tracked = isinstance(order, dict) and order.get("order_id") is not None
    delay = SECURITY_META.get(ticker, {}).get("delay", 0.0)
    started = time.time()

    return {
        "ticker": ticker,
        "action": action,
        "type": order_type,
        "quantity": quantity,
        "price": price,
        "position_before": position_before,
        "order": order if tracked else {},
        "tracked": tracked,
        "cancel_at": (
            float("inf") if order_type == "MARKET"
            else started + delay + IOC_CANCEL_AFTER
        ),
        "give_up_at": started + delay + ORDER_MAX_WAIT,
        "cancel_sent": False,
        "unresolved": False,
    }


def _order_open(handle):

    if not handle["tracked"]:
        return True

    return str(handle["order"].get("status", "")).upper() == "OPEN"


def resolve_orders(handles):
    """
    Waits until every submitted order is filled or cancelled,
    then syncs positions. Returns [(filled, vwap), ...]
    aligned with `handles`; a None handle gives (0, None).

    Nothing new is sent while this runs, which is what stops
    the algorithm from firing again on a book that still
    shows an edge it has already traded.
    """

    active = [h for h in handles if h is not None]

    while True:

        now = time.time()
        waiting = []

        for handle in active:

            if not _order_open(handle):
                continue

            if now >= handle["cancel_at"] and not handle["cancel_sent"]:

                if handle["tracked"]:
                    try:
                        cancel_order(handle["order"]["order_id"])
                    except Exception:
                        pass  # may have filled meanwhile

                handle["cancel_sent"] = True

            if now >= handle["give_up_at"] or (
                not handle["tracked"] and handle["cancel_sent"]
            ):
                handle["unresolved"] = True
                continue

            waiting.append(handle)

        if not waiting:
            break

        time.sleep(0.02)

        for handle in waiting:

            if not handle["tracked"]:
                continue

            try:
                handle["order"] = get_order(handle["order"]["order_id"])
            except Exception:
                pass

    results = []

    for handle in handles:

        if handle is None:
            results.append((0, None))
            continue

        ticker = handle["ticker"]

        if handle["unresolved"]:

            # Could not confirm through /orders/{id}: cancel a
            # LIMIT remainder and infer the fill from position.
            if handle["tracked"] and handle["type"] == "LIMIT":
                try:
                    cancel_order(handle["order"]["order_id"])
                except Exception:
                    pass

            time.sleep(0.2)
            filled = abs(get_position(ticker) - handle["position_before"])

            print(
                f"ORDER UNCONFIRMED | {ticker} | "
                f"fill inferred from position: {filled:,}"
            )

            results.append((int(filled), None))
            continue

        filled = int(round(float(handle["order"].get("quantity_filled") or 0)))
        vwap = handle["order"].get("vwap")
        vwap = float(vwap) if vwap is not None else None

        if filled > 0:
            signed = filled if handle["action"] == "BUY" else -filled
            wait_for_position(ticker, handle["position_before"] + signed)

        results.append((filled, vwap))

    return results


def execute_ioc(ticker, action, quantity, price):
    """
    One marketable LIMIT, remainder cancelled. Blocks until
    resolved and synced. Returns (filled_quantity, vwap).
    """

    return resolve_orders([submit_order(ticker, action, quantity, price, "LIMIT")])[0]


# ============================================================
# PING-PONG DETECTOR
# ============================================================

def register_direction(base, action):
    """
    Returns False (and halts the security) when unwind orders
    keep reversing direction inside one episode.
    """

    last = LAST_DIRECTION.get(base)

    if last is not None and last != action:

        DIRECTION_FLIPS[base] = DIRECTION_FLIPS.get(base, 0) + 1

        print(
            f"DIRECTION FLIP | {base} | {last} -> {action} | "
            f"flips {DIRECTION_FLIPS[base]}"
        )

        if DIRECTION_FLIPS[base] > MAX_DIRECTION_FLIPS:

            HALTED_BASES.add(base)

            print("\n" + "!" * 70)
            print(f"{base} HALTED: unwind kept reversing direction.")
            print("The position feed and the fills disagree.")
            print(f"Unwind {base} MANUALLY for the rest of the round.")
            print("!" * 70)

            return False

    LAST_DIRECTION[base] = action

    return True


def end_episode(base):

    LAST_DIRECTION.pop(base, None)
    DIRECTION_FLIPS[base] = 0


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
    HALT_REASON = (
        f"NLV {nlv:,.0f} is {drawdown:,.0f} below "
        f"its peak {PEAK_NLV:,.0f}"
    )

    try:
        cancel_all_orders()
    except Exception:
        pass

    print("\n" + "!" * 70)
    print("KILL SWITCH: automated trading stopped for this round.")
    print(HALT_REASON)
    print("Positions are NOT flattened automatically.")
    print("Check the portfolio and decide manually.")
    print("!" * 70)

    return True


# ============================================================
# UNWIND
# ============================================================

def plan_children(pair, action, quantity, mode, band):
    """
    Splits one unwind step across Main and Alternative.

    LIMIT : each child sized to the top level of its venue.
    MARKET: each child sized to the depth within `band` of
            the best price of its venue.

    The best fee-adjusted venue gets the first child; the
    other venue gets one too when its price is within
    SECOND_VENUE_TOLERANCE.
    """

    side = "bids" if action == "SELL" else "asks"

    venues = []

    for ticker in (pair["main"], pair["alternative"]):

        try:
            book = get_order_book(ticker, limit=BOOK_LIMIT)
        except Exception:
            continue

        best = get_best_order(book, side)

        if best is None:
            continue

        if mode == "MARKET":
            capacity = depth_within(book, side, best["price"], band)
        else:
            capacity = best["quantity"]

        fee = fee_for(ticker)

        venues.append({
            "ticker": ticker,
            "price": best["price"],
            "capacity": int(min(capacity, order_size_cap(ticker))),
            "effective": (
                best["price"] - fee if action == "SELL"
                else best["price"] + fee
            ),
        })

    if not venues:
        return []

    venues.sort(key=lambda venue: venue["effective"], reverse=(action == "SELL"))

    best_effective = venues[0]["effective"]
    children = []
    left = int(quantity)

    for venue in venues:

        if left <= 0:
            break

        if children and abs(venue["effective"] - best_effective) > SECOND_VENUE_TOLERANCE:
            break

        size = min(left, venue["capacity"])

        if size <= 0:
            continue

        children.append({
            "ticker": venue["ticker"],
            "price": venue["price"],
            "quantity": size,
        })

        left -= size

    return children


def unwind_step(base, pair, delta, mode="LIMIT", band=MARKET_SLIPPAGE_BAND, track=True):
    """
    One step moving NET exposure of `base` toward the target
    (delta = current - target): up to one child per venue,
    all resolved and synced before returning.

    Returns (signed_filled, requested, cash):
        signed_filled  > 0 bought, < 0 sold
        requested      shares sent
        cash           cash flow net of taker fees
    """

    if delta == 0:
        return 0, 0, 0.0

    action = "SELL" if delta > 0 else "BUY"

    children = plan_children(pair, action, abs(delta), mode, band)

    if not children:
        print(f"UNWIND WAIT | {base} | no liquidity")
        return 0, 0, 0.0

    if track and not register_direction(base, action):
        return 0, 0, 0.0

    requested = sum(child["quantity"] for child in children)

    print(
        f"UNWIND {mode} | {base} | {action} "
        + " + ".join(
            f"{child['quantity']:,} {child['ticker']} @ {child['price']:.2f}"
            for child in children
        )
        + f" | remaining {delta:+,}"
    )

    handles = [
        submit_order(child["ticker"], action, child["quantity"], child["price"], mode)
        for child in children
    ]

    results = resolve_orders(handles)

    filled_total = 0
    cash = 0.0
    parts = []

    for child, (filled, vwap) in zip(children, results):

        if filled <= 0:
            continue

        price = vwap if vwap is not None else child["price"]
        fee = fee_for(child["ticker"])

        if action == "SELL":
            cash += filled * (price - fee)
        else:
            cash -= filled * (price + fee)

        filled_total += filled
        parts.append(f"{filled:,} {child['ticker']} @ {price:.4f}")

    if parts:
        print("  filled " + " + ".join(parts))

    signed = filled_total if action == "BUY" else -filled_total

    return signed, requested, cash


# ============================================================
# ACTIVE TENDER
# ============================================================

def process_active_tender(current_tick):
    """
    Unwinds ACTIVE_TENDER["base"] back to position_before.

    LIMIT at the top of book while exposure is short; MARKET
    once the inventory has been held UNWIND_MARKET_AFTER_TICKS
    ticks or LIMIT children keep missing; wider MARKET band
    after UNWIND_MAX_TICKS.
    """

    global ACTIVE_TENDER

    if ACTIVE_TENDER is None or HALTED:
        return

    state = ACTIVE_TENDER
    base = state["base"]
    target = int(state["position_before"])

    if base in HALTED_BASES:
        print(f"TENDER RELEASED | {base} is halted: unwind manually")
        ACTIVE_TENDER = None
        return

    securities = synced_securities()
    pair = get_market_pairs(securities).get(base)

    if pair is None:
        print(f"TENDER UNWIND WAIT | {base} market pair not available")
        return

    live = get_base_position(base, securities)

    # --------------------------------------------------------
    # WAIT FOR TENDER INVENTORY
    # --------------------------------------------------------

    if state.get("state") == "WAITING_FOR_TENDER":

        started = state.setdefault("wait_started", time.time())

        if live == target:

            if time.time() - started > TENDER_APPEAR_TIMEOUT:
                print(
                    f"\nTENDER INVENTORY NEVER APPEARED | {base} | "
                    f"lock released"
                )
                ACTIVE_TENDER = None

            return

        observed = live - target
        expected = int(state.get("expected_after", live)) - target

        print(
            f"\nTENDER INVENTORY | {base} | before {target:+,} | "
            f"now {live:+,} | delta {observed:+,}"
        )

        if expected and abs(abs(observed) - abs(expected)) > 0.05 * abs(expected):
            print(
                f"WARNING | {base} | position change {observed:+,} "
                f"differs from tender quantity {expected:+,}. "
                f"Check how RIT reports _M / _A positions."
            )

        state["inventory_tick"] = current_tick
        end_episode(base)

    # v1 states, and auction wins handed over by iteration_6,
    # map to UNWINDING.
    state["state"] = "UNWINDING"
    state.setdefault("inventory_tick", current_tick)
    state.setdefault("ioc_misses", 0)

    delta = live - target
    ticks_held = current_tick - state["inventory_tick"]

    if delta == 0:
        print(
            f"\nTENDER COMPLETE | {base} | back to {target:+,} | "
            f"{ticks_held} ticks exposed"
        )
        end_episode(base)
        ACTIVE_TENDER = None
        return

    # --------------------------------------------------------
    # LIMIT OR MARKET
    # --------------------------------------------------------

    urgent = ticks_held >= UNWIND_MAX_TICKS

    if urgent or ticks_held >= UNWIND_MARKET_AFTER_TICKS:
        mode = "MARKET"
        reason = f"{ticks_held} ticks exposed"

    elif state["ioc_misses"] >= IOC_MISSES_BEFORE_MARKET:
        mode = "MARKET"
        reason = f"{state['ioc_misses']} LIMIT children in a row not fully filled"

    else:
        mode = "LIMIT"
        reason = None

    band = URGENT_SLIPPAGE_BAND if urgent else MARKET_SLIPPAGE_BAND

    if state.get("mode_key") != (mode, urgent):

        state["mode_key"] = (mode, urgent)

        if mode == "MARKET":
            print(
                f"UNWIND -> MARKET{' (URGENT)' if urgent else ''} | {base} | "
                f"{reason} | band ${band:.2f}"
            )

    signed, requested, _ = unwind_step(base, pair, delta, mode, band)

    if mode == "LIMIT" and requested:

        if abs(signed) < requested:
            state["ioc_misses"] += 1
        else:
            state["ioc_misses"] = 0


# ============================================================
# TENDERS
# ============================================================

def process_tenders(processed_tenders, securities, current_tick):

    global ACTIVE_TENDER

    if ACTIVE_TENDER is not None or HALTED:
        return

    for tender in get_tenders():

        tender_id = tender["tender_id"]

        if tender_id in processed_tenders:
            continue

        # Round 5 = fixed tenders only.
        if tender.get("is_fixed_bid", True) is False:
            continue

        try:

            result = engine.evaluate_tender(tender, securities)
            engine.print_tender_result(result)

            if result["decision"] != "ACCEPT":

                if not DRY_RUN:
                    decline_tender(tender_id)

                processed_tenders.add(tender_id)
                print("\nTender declined.")
                return

            base = get_base_ticker(tender["ticker"])
            action = str(tender["action"]).upper()
            quantity = int(tender["quantity"])

            if action not in ("BUY", "SELL"):

                print(f"Unknown tender action: {action}")

                if not DRY_RUN:
                    decline_tender(tender_id)

                processed_tenders.add(tender_id)
                return

            position_before = get_live_base_position(base)

            expected_after = (
                position_before + quantity if action == "BUY"
                else position_before - quantity
            )

            ACTIVE_TENDER = {
                "tender_id": tender_id,
                "base": base,
                "tender_action": action,
                "tender_quantity": quantity,
                "position_before": position_before,
                "expected_after": expected_after,
                "accepted_tick": current_tick,
                "state": "WAITING_FOR_TENDER",
            }

            try:
                if not DRY_RUN:
                    accept_tender(tender_id)
            except Exception:
                ACTIVE_TENDER = None
                raise

            processed_tenders.add(tender_id)

            print("\n" + "=" * 70)
            print("TENDER ACCEPTED - ARBITRAGE LOCKED")
            print(f"Security:             {base}")
            print(f"Tender:               {action} {quantity:,}")
            print(f"Position before:      {position_before:+,}")
            print(f"Expected after:       {expected_after:+,}")
            print(f"UNWIND TARGET:        {position_before:+,}")
            print("=" * 70)

            return

        except Exception as error:

            print(f"Tender {tender_id} error: {error}")
            return


# ============================================================
# RESIDUAL FLATTENER
# ============================================================

def flatten_residuals(securities, pairs):
    """
    Any NET exposure left with no tender active (a broken
    arbitrage leg, a lock lost on PAUSE, an auction filled
    late) is unwanted risk: flatten it with MARKET.

    Returns True if it traded this call.
    """

    for base, pair in pairs.items():

        if base in HALTED_BASES:
            continue

        net = get_base_position(base, securities)

        if net == 0:

            if base in LAST_DIRECTION:
                end_episode(base)

            continue

        print(f"\nRESIDUAL NET EXPOSURE | {base} | {net:+,} -> flattening (MARKET)")
        unwind_step(base, pair, net, "MARKET", MARKET_SLIPPAGE_BAND)

        return True

    return False


# ============================================================
# ARBITRAGE
# ============================================================

def build_opportunity(buy_ticker, ask, sell_ticker, bid):

    net_edge = (
        (bid["price"] - fee_for(sell_ticker))
        - (ask["price"] + fee_for(buy_ticker))
    )

    quantity = int(min(ask["quantity"], bid["quantity"], MAX_ARBITRAGE_SIZE))

    if quantity <= 0:
        return None

    return {
        "base": get_base_ticker(buy_ticker),
        "buy_ticker": buy_ticker,
        "buy_price": ask["price"],
        "sell_ticker": sell_ticker,
        "sell_price": bid["price"],
        "quantity": quantity,
        "net_edge": net_edge,
    }


def find_arbitrage(main_ticker, alternative_ticker):
    """
    Cross-venue arbitrage on top of book, net of taker fees.
    Size = smaller of the two top levels (MAX_ARBITRAGE_SIZE
    at most), so the MARKET legs do not walk the book unless
    it moves before they execute.
    """

    main_book = get_order_book(main_ticker, limit=5)
    alt_book = get_order_book(alternative_ticker, limit=5)

    main_bid = get_best_order(main_book, "bids")
    main_ask = get_best_order(main_book, "asks")
    alt_bid = get_best_order(alt_book, "bids")
    alt_ask = get_best_order(alt_book, "asks")

    options = []

    if alt_ask and main_bid:
        options.append(
            build_opportunity(alternative_ticker, alt_ask, main_ticker, main_bid)
        )

    if main_ask and alt_bid:
        options.append(
            build_opportunity(main_ticker, main_ask, alternative_ticker, alt_bid)
        )

    options = [
        option for option in options
        if option is not None
        and option["net_edge"] >= MIN_ARBITRAGE_NET_EDGE
    ]

    if not options:
        return None

    return max(options, key=lambda option: option["net_edge"])


def arbitrage_size_allowed(opportunity, securities):
    """
    Caps the size by per-ticker exposure and by RIT's own
    gross limit (each arbitrage adds 2 x quantity of gross).
    """

    quantity = int(opportunity["quantity"])

    buy_position = ticker_position(opportunity["buy_ticker"], securities)
    sell_position = ticker_position(opportunity["sell_ticker"], securities)

    quantity = min(
        quantity,
        MAX_VENUE_POSITION - buy_position,
        MAX_VENUE_POSITION + sell_position,
    )

    if quantity <= 0:
        return 0

    try:
        limits = get_limits()
    except Exception:
        limits = []

    for limit in limits if isinstance(limits, list) else []:

        if not isinstance(limit, dict):
            continue

        gross_limit = float(limit.get("gross_limit") or 0)
        gross = float(limit.get("gross") or 0)

        if gross_limit > 0:
            headroom = 0.95 * gross_limit - gross
            quantity = min(quantity, int(headroom // 2))

    return max(int(quantity), 0)


def execute_arbitrage(opportunity, quantity, pair):

    global ARB_PNL, ARB_CONSECUTIVE_LOSSES, ARB_DISABLED

    base = opportunity["base"]
    buy_ticker = opportunity["buy_ticker"]
    sell_ticker = opportunity["sell_ticker"]
    buy_price = opportunity["buy_price"]
    sell_price = opportunity["sell_price"]

    print(
        f"\nARBITRAGE | {base} | {quantity:,} | "
        f"edge ${opportunity['net_edge']:.4f}/share"
    )
    print(f"  BUY  MARKET {buy_ticker}  (seen {buy_price:.2f})")
    print(f"  SELL MARKET {sell_ticker}  (seen {sell_price:.2f})")

    # --------------------------------------------------------
    # BOTH LEGS AT ONCE (MARKET / MARKET), then wait for both
    # to resolve before looking at the book again.
    # --------------------------------------------------------

    buy_handle = submit_order(buy_ticker, "BUY", quantity, order_type="MARKET")

    sell_handle = (
        submit_order(sell_ticker, "SELL", quantity, order_type="MARKET")
        if buy_handle is not None else None
    )

    (bought, buy_vwap), (sold, sell_vwap) = resolve_orders([buy_handle, sell_handle])

    if bought == 0 and sold == 0:
        print("  no fills")
        return

    cash = 0.0

    if bought:
        cash -= bought * ((buy_vwap if buy_vwap is not None else buy_price) + fee_for(buy_ticker))

    if sold:
        cash += sold * ((sell_vwap if sell_vwap is not None else sell_price) - fee_for(sell_ticker))

    # --------------------------------------------------------
    # HEDGE ANY DIFFERENCE BETWEEN THE TWO FILLS (MARKET)
    # --------------------------------------------------------

    imbalance = bought - sold

    if imbalance:
        print(f"  legs filled {bought:,} / {sold:,}: hedging {imbalance:+,} with MARKET")

    for _ in range(ARB_HEDGE_ATTEMPTS):

        if imbalance == 0:
            break

        signed, _, hedge_cash = unwind_step(
            base, pair, imbalance, "MARKET", URGENT_SLIPPAGE_BAND, track=False
        )

        imbalance += signed
        cash += hedge_cash

    # --------------------------------------------------------
    # RESULT (any unhedged rest marked at the observed mid)
    # --------------------------------------------------------

    pnl = cash + imbalance * (buy_price + sell_price) / 2

    ARB_PNL += pnl
    ARB_CONSECUTIVE_LOSSES = ARB_CONSECUTIVE_LOSSES + 1 if pnl < 0 else 0

    print(
        f"  bought {bought:,} @ {buy_vwap or 0:.4f} | "
        f"sold {sold:,} @ {sell_vwap or 0:.4f} | "
        f"pnl ${pnl:,.2f} | arbitrage total ${ARB_PNL:,.2f}"
    )

    if imbalance != 0:
        print(f"  UNHEDGED {imbalance:+,} {base}: residual flattener takes it")

    if (
        ARB_PNL <= -ARB_MAX_LOSS
        or ARB_CONSECUTIVE_LOSSES >= ARB_MAX_CONSECUTIVE_LOSSES
    ):

        ARB_DISABLED = True

        print("\n" + "!" * 70)
        print(
            f"ARBITRAGE DISABLED FOR THIS ROUND | total ${ARB_PNL:,.2f} | "
            f"losses in a row {ARB_CONSECUTIVE_LOSSES}"
        )
        print("!" * 70)


def process_arbitrage(securities):
    """
    Runs only with no tender active. First flattens any
    residual net exposure, then looks for one arbitrage.
    """

    if HALTED or ACTIVE_TENDER is not None:
        return

    securities = synced_securities()
    pairs = get_market_pairs(securities)

    if flatten_residuals(securities, pairs):
        return

    if not ARBITRAGE_ENABLED or ARB_DISABLED:
        return

    for base, pair in pairs.items():

        if base in HALTED_BASES:
            continue

        try:

            opportunity = find_arbitrage(pair["main"], pair["alternative"])

            if opportunity is None:
                continue

            quantity = arbitrage_size_allowed(opportunity, securities)

            if quantity <= 0:
                continue

            execute_arbitrage(opportunity, quantity, pair)
            time.sleep(ARBITRAGE_COOLDOWN)

            return

        except Exception as error:

            print(f"Arbitrage error {base}: {error}")
            return


# ============================================================
# ITERATION 5 MAIN LOOP
# ============================================================

def run_iteration_5(config):

    reset_runtime_state()
    configure_round_5_engine()
    load_security_meta()

    print("=" * 70)
    print("ITERATION 5 (v3)")
    print("TENDER-LINKED UNWIND + ARBITRAGE")
    print("=" * 70)
    print("Arbitrage:             MARKET / MARKET, legs together")
    print("Arb Fill Difference:   HEDGED AT ONCE (MARKET)")
    print("Tender Unwind:         LIMIT -> MARKET")
    print(f"  MARKET after:        {UNWIND_MARKET_AFTER_TICKS} ticks exposed "
          f"or {IOC_MISSES_BEFORE_MARKET} LIMIT misses")
    print(f"  Urgent after:        {UNWIND_MAX_TICKS} ticks "
          f"(band ${URGENT_SLIPPAGE_BAND:.2f})")
    print(f"  MARKET band:         ${MARKET_SLIPPAGE_BAND:.2f}")
    print("Residual Net:          FLATTENED WHEN IDLE (MARKET)")
    print(f"Max Venue Position:    {MAX_VENUE_POSITION:,}")
    print(f"Arb Loss Limit:        ${ARB_MAX_LOSS:,.0f} or "
          f"{ARB_MAX_CONSECUTIVE_LOSSES} losses in a row")
    print(f"Kill Switch Drawdown:  {MAX_DRAWDOWN}")
    print(f"Minimum Arb Edge:      ${MIN_ARBITRAGE_NET_EDGE:.3f}/share")
    print("=" * 70)
    print_security_meta()

    processed_tenders = set()

    while True:

        try:

            case_info = get_case()

            if get_round_number(case_info) != config["iteration"]:
                print(f"\nIteration {config['iteration']} ended.")
                return

            if case_info.get("status") != "ACTIVE":
                return

            current_tick = int(case_info.get("tick", 0))

            securities = get_securities()
            engine.update_market_history(securities, current_tick)

            if check_kill_switch():
                time.sleep(0.25)
                continue

            # PRIORITY 1: finish the active tender.
            if ACTIVE_TENDER is not None:
                process_active_tender(current_tick)
                time.sleep(POLL_INTERVAL)
                continue

            # PRIORITY 2: new tender.
            process_tenders(processed_tenders, securities, current_tick)

            if ACTIVE_TENDER is not None:
                time.sleep(POLL_INTERVAL)
                continue

            # PRIORITY 3: residual flattening, then arbitrage.
            process_arbitrage(securities)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:

            print("\nIteration 5 stopped by user.")
            return

        except Exception as error:

            print(f"\nIteration 5 error: {error}")
            time.sleep(POLL_INTERVAL)