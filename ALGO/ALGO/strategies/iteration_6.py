# ITERATION 6 - TENDER DECISIONS, AUCTION BIDS, PURE ARBITRAGE
# Requires the companion iteration_5_pure_arbitrage.py installed as strategies/iteration_5.py.
# Tender inventory deliberately remains open; arbitrage order legs alone may trade.
# Auctions remain priced/bid using the existing evaluator, but are never unwound.

import time
import math

from api import (
    get_case,
    get_round_number,
    get_tenders,
    get_securities,
    get_order_book,
    accept_tender,
    decline_tender
)

import strategies.iteration_2 as tender_engine
import strategies.iteration_5 as trading_engine


# ============================================================
# GENERAL SETTINGS
# ============================================================

DRY_RUN = False

POLL_INTERVAL = 0.05

BOOK_LIMIT = 100


# ============================================================
# ROUND 6 FEES
# ============================================================

MAIN_FEE = 0.02

ALTERNATIVE_FEE = 0.01

ALTERNATIVE_REBATE = 0.005


# ============================================================
# TENDER RISK PARAMETERS
# ============================================================

LAMBDA_INVENTORY = 0.002

LAMBDA_LIQUIDATION = 0.35

MIN_VISIBLE_COVERAGE = 0.25

MIN_EXPECTED_PROFIT = 0.0


# ============================================================
# AUCTION PARAMETERS
# ============================================================

AUCTION_MIN_PROFIT_PER_SHARE = 0.03

AUCTION_SUBMIT_TICKS_BEFORE_EXPIRY = 1


# After expiry, allow a little time for the auction result
# / resulting inventory to appear.
AUCTION_RESULT_GRACE_TICKS = 2


# ============================================================
# ARBITRAGE PARAMETERS
# ============================================================

ARBITRAGE_ENABLED = True

MIN_ARBITRAGE_NET_EDGE = 0.05

MAX_ARBITRAGE_SIZE = 1_000

ARBITRAGE_COOLDOWN = 0.10


# ============================================================
# POSITION LIMITS
# ============================================================

NET_POSITION_LIMIT = 100_000

GROSS_POSITION_LIMIT = 200_000


# ============================================================
# AUCTION STATE
# ============================================================
#
# No trading or unwind lock is created.
# when an auction bid is submitted.
#
# Why?
#
# Because submitting a bid does not mean we won.
#
# PENDING_AUCTION:
#
# {
#     "tender_id": ...,
#     "base": ...,
#     "action": ...,
#     "quantity": ...,
#     "price": ...,
#     "position_before": ...,
#     "expected_after": ...,
#     "expiry_tick": ...
# }
#
# ============================================================

PENDING_AUCTION = None


# ============================================================
# RESET STATE
# ============================================================

def reset_runtime_state():

    global PENDING_AUCTION

    PENDING_AUCTION = None

    trading_engine.reset_runtime_state()


# ============================================================
# CONFIGURE ROUND 6 ENGINES
# ============================================================

def configure_round_6_engines():

    # ========================================================
    # TENDER ENGINE
    # ========================================================

    tender_engine.MAIN_FEE = (
        MAIN_FEE
    )

    tender_engine.ALTERNATIVE_FEE = (
        ALTERNATIVE_FEE
    )

    tender_engine.ALTERNATIVE_REBATE = (
        ALTERNATIVE_REBATE
    )

    tender_engine.LAMBDA_INVENTORY = (
        LAMBDA_INVENTORY
    )

    tender_engine.LAMBDA_LIQUIDATION = (
        LAMBDA_LIQUIDATION
    )

    tender_engine.MIN_VISIBLE_COVERAGE = (
        MIN_VISIBLE_COVERAGE
    )

    tender_engine.MIN_EXPECTED_PROFIT = (
        MIN_EXPECTED_PROFIT
    )

    tender_engine.NET_POSITION_LIMIT = (
        NET_POSITION_LIMIT
    )

    tender_engine.GROSS_POSITION_LIMIT = (
        GROSS_POSITION_LIMIT
    )


    # ========================================================
    # ITERATION 5 TRADING ENGINE
    # ========================================================

    trading_engine.DRY_RUN = (
        DRY_RUN
    )

    trading_engine.MAIN_FEE = (
        MAIN_FEE
    )

    trading_engine.ALTERNATIVE_FEE = (
        ALTERNATIVE_FEE
    )

    trading_engine.ALTERNATIVE_REBATE = (
        ALTERNATIVE_REBATE
    )

    trading_engine.ARBITRAGE_ENABLED = (
        ARBITRAGE_ENABLED
    )

    trading_engine.MIN_ARBITRAGE_NET_EDGE = (
        MIN_ARBITRAGE_NET_EDGE
    )

    trading_engine.MAX_ARBITRAGE_SIZE = (
        MAX_ARBITRAGE_SIZE
    )

    # ========================================================
    # IMPORTANT
    #
    # Both iterations share the iteration_2 tender evaluator.
    #
    # Reconfigure it for Round 6.
    # ========================================================

    trading_engine.configure_round_5_engine()

    # configure_round_5_engine() applies Round 5 fee globals,
    # so override the shared tender engine once more.

    tender_engine.MAIN_FEE = (
        MAIN_FEE
    )

    tender_engine.ALTERNATIVE_FEE = (
        ALTERNATIVE_FEE
    )

    tender_engine.ALTERNATIVE_REBATE = (
        ALTERNATIVE_REBATE
    )

    tender_engine.LAMBDA_INVENTORY = (
        LAMBDA_INVENTORY
    )

    tender_engine.LAMBDA_LIQUIDATION = (
        LAMBDA_LIQUIDATION
    )

    tender_engine.MIN_VISIBLE_COVERAGE = (
        MIN_VISIBLE_COVERAGE
    )

    tender_engine.MIN_EXPECTED_PROFIT = (
        MIN_EXPECTED_PROFIT
    )

    tender_engine.NET_POSITION_LIMIT = (
        NET_POSITION_LIMIT
    )

    tender_engine.GROSS_POSITION_LIMIT = (
        GROSS_POSITION_LIMIT
    )


# ============================================================
# BASIC HELPERS
# ============================================================

def get_base_ticker(ticker):

    return trading_engine.get_base_ticker(
        ticker
    )


def get_base_position(
    base,
    securities
):

    return trading_engine.get_base_position(
        base,
        securities
    )


def get_live_base_position(base):

    return trading_engine.get_live_base_position(
        base
    )


# ============================================================
# ROUNDING
# ============================================================

def round_down_cent(price):

    return (
        math.floor(
            price * 100
            + 1e-9
        )
        /
        100
    )


def round_up_cent(price):

    return (
        math.ceil(
            price * 100
            - 1e-9
        )
        /
        100
    )


# ============================================================
# AUCTION TYPE
# ============================================================

def get_auction_description(
    tender
):

    caption = str(
        tender.get(
            "caption",
            ""
        )
    ).lower()


    if "winner" in caption:

        return "WINNER-TAKE-ALL"


    if "competitive" in caption:

        return "COMPETITIVE"


    return "AUCTION"


# ============================================================
# AUCTION EXPIRY
# ============================================================

def get_auction_expiry_tick(
    tender
):

    expires = (
        tender.get(
            "expires"
        )
    )


    if expires is None:

        return None


    try:

        return int(
            expires
        )


    except (
        TypeError,
        ValueError
    ):

        return None


# ============================================================
# RIT LIMITS CHECK (gross counts |_M| + |_A|)
# ============================================================

def fits_rit_limits(
    tender,
    processed_tenders
):
    """
    Declines the tender when RIT's own gross / net headroom
    (iteration_5.tender_fits_limits) cannot take it. Returns
    True when it fits.
    """

    quantity = int(
        tender[
            "quantity"
        ]
    )

    signed = (
        quantity
        if str(tender["action"]).upper() == "BUY"
        else -quantity
    )

    trading_engine.invalidate_limits_cache()

    fits, why = (
        trading_engine.tender_fits_limits(
            signed
        )
    )

    if fits:

        return True


    print(
        f"\n>>>>> DECLINE <<<<<\n"
        f"Reason: RIT limits: {why}"
    )

    print(
        f"Current: "
        f"{trading_engine.format_limits()}"
    )


    if not DRY_RUN:

        decline_tender(
            tender[
                "tender_id"
            ]
        )


    processed_tenders.add(
        tender[
            "tender_id"
        ]
    )

    return False


# ============================================================
# CREATE FIXED-TENDER LOCK
# ============================================================

def create_fixed_tender_lock(tender, current_tick):
    """No lock or unwind: record tender exposure only for diagnostics."""
    base = get_base_ticker(tender["ticker"])
    before = get_live_base_position(base)
    signed = int(tender["quantity"]) * (1 if str(tender["action"]).upper() == "BUY" else -1)
    return base, before, before + signed

def evaluate_auction(
    tender,
    securities
):
    """
    Calculate auction price using current Main + Alternative
    books.

    BUY:
        client sells to us
        -> we become long
        -> expected unwind = sell into bids

    SELL:
        client buys from us
        -> we become short
        -> expected unwind = buy from asks
    """

    tender_id = (
        tender[
            "tender_id"
        ]
    )


    tender_ticker = (
        tender[
            "ticker"
        ]
    )


    action = str(
        tender[
            "action"
        ]
    ).upper()


    quantity = int(
        tender[
            "quantity"
        ]
    )


    base = (
        tender_engine.get_base_ticker(
            tender_ticker
        )
    )


    auction_type = (
        get_auction_description(
            tender
        )
    )


    # ========================================================
    # RESOLVE VENUES
    # ============================================================

    (
        main_ticker,
        alternative_ticker

    ) = tender_engine.resolve_market_tickers(

        tender_ticker,

        securities
    )


    # ========================================================
    # INVENTORY
    # ============================================================

    inventory_before = (
        tender_engine.get_total_position(

            base,

            securities
        )
    )


    if action == "BUY":

        inventory_after = (
            inventory_before
            +
            quantity
        )


    elif action == "SELL":

        inventory_after = (
            inventory_before
            -
            quantity
        )


    else:

        raise ValueError(
            f"Unknown auction action: "
            f"{action}"
        )


    # ========================================================
    # POSITION LIMIT CHECK
    # ============================================================

    positions = (
        tender_engine.get_all_positions(
            securities
        )
    )


    (
        within_limits,
        projected_net,
        projected_gross

    ) = tender_engine.check_position_limits(

        positions,

        base,

        inventory_after
    )


    if not within_limits:

        return {

            "tender_id":
                tender_id,

            "ticker":
                base,

            "action":
                action,

            "quantity":
                quantity,

            "auction_type":
                auction_type,

            "decision":
                "DECLINE",

            "auction_price":
                None,

            "reason":
                "Position limit violation"
        }


    # ========================================================
    # LATEST ORDER BOOKS
    # ============================================================

    main_book = (
        get_order_book(

            main_ticker,

            limit=
                BOOK_LIMIT
        )
    )


    alternative_book = (
        get_order_book(

            alternative_ticker,

            limit=
                BOOK_LIMIT
        )
    )


    # ========================================================
    # EXPECTED UNWIND VWAP
    # ============================================================

    (
        unwind_vwap,
        executions,
        visible_depth

    ) = tender_engine.calculate_executable_vwap(

        main_book,

        alternative_book,

        action,

        quantity
    )


    coverage_ratio = (

        visible_depth
        /
        float(
            quantity
        )

        if quantity > 0

        else 0.0
    )


    if unwind_vwap is None:

        return {

            "tender_id":
                tender_id,

            "ticker":
                base,

            "action":
                action,

            "quantity":
                quantity,

            "auction_type":
                auction_type,

            "visible_depth":
                visible_depth,

            "coverage_ratio":
                coverage_ratio,

            "decision":
                "DECLINE",

            "auction_price":
                None,

            "reason":
                (
                    "Visible market coverage below "
                    f"{MIN_VISIBLE_COVERAGE:.0%}"
                )
        }


    # ========================================================
    # TRANSACTION COST
    # ============================================================

    transaction_cost = (
        tender_engine.calculate_transaction_cost(

            executions,

            target_quantity=
                quantity
        )
    )


    # ========================================================
    # INVENTORY RISK
    # ============================================================

    inventory_risk = (
        tender_engine.calculate_inventory_risk(

            inventory_before,

            inventory_after
        )
    )


    # ========================================================
    # VOLATILITY
    # ============================================================

    volatility = (
        tender_engine.calculate_short_term_volatility(
            base
        )
    )


    # ========================================================
    # LIQUIDATION RISK
    # ============================================================

    liquidation_risk = (
        tender_engine.calculate_liquidation_risk(

            quantity,

            visible_depth,

            volatility
        )
    )


    # ========================================================
    # REQUIRED PROFIT
    # ============================================================

    required_profit = (

        AUCTION_MIN_PROFIT_PER_SHARE

        *

        quantity
    )


    total_adjustment = (

        transaction_cost

        +

        inventory_risk

        +

        liquidation_risk

        +

        required_profit
    )


    adjustment_per_share = (

        total_adjustment

        /

        quantity
    )


    # ========================================================
    # BUY AUCTION
    # ========================================================

    if action == "BUY":

        raw_price = (

            unwind_vwap

            -

            adjustment_per_share
        )


        auction_price = (
            round_down_cent(
                raw_price
            )
        )


        if auction_price <= 0:

            return {

                "tender_id":
                    tender_id,

                "ticker":
                    base,

                "action":
                    action,

                "quantity":
                    quantity,

                "auction_type":
                    auction_type,

                "decision":
                    "DECLINE",

                "auction_price":
                    None,

                "reason":
                    "Invalid auction price"
            }


        expected_profit = (

            (
                unwind_vwap
                -
                auction_price
            )

            *
            quantity

            -

            transaction_cost

            -

            inventory_risk

            -

            liquidation_risk
        )


    # ========================================================
    # SELL AUCTION
    # ============================================================

    else:

        raw_price = (

            unwind_vwap

            +

            adjustment_per_share
        )


        auction_price = (
            round_up_cent(
                raw_price
            )
        )


        expected_profit = (

            (
                auction_price
                -
                unwind_vwap
            )

            *
            quantity

            -

            transaction_cost

            -

            inventory_risk

            -

            liquidation_risk
        )


    expected_profit_per_share = (

        expected_profit

        /

        quantity
    )


    return {

        "tender_id":
            tender_id,

        "ticker":
            base,

        "main_ticker":
            main_ticker,

        "alternative_ticker":
            alternative_ticker,

        "auction_type":
            auction_type,

        "action":
            action,

        "quantity":
            quantity,

        "unwind_vwap":
            unwind_vwap,

        "auction_price":
            auction_price,

        "transaction_cost":
            transaction_cost,

        "inventory_before":
            inventory_before,

        "inventory_after":
            inventory_after,

        "inventory_risk":
            inventory_risk,

        "volatility":
            volatility,

        "visible_depth":
            visible_depth,

        "coverage_ratio":
            coverage_ratio,

        "liquidation_risk":
            liquidation_risk,

        "required_profit":
            required_profit,

        "expected_profit":
            expected_profit,

        "expected_profit_per_share":
            expected_profit_per_share,

        "projected_net":
            projected_net,

        "projected_gross":
            projected_gross,

        "executions":
            executions,

        "decision":
            "SUBMIT",

        "reason":
            "Auction price calculated from latest market"
    }


# ============================================================
# PRINT AUCTION
# ============================================================

def print_auction_result(
    result
):

    print(
        "\n"
        + "=" * 70
    )


    print(
        f"{result['auction_type']} "
        f"{result['tender_id']}"
    )


    print(
        "=" * 70
    )


    print(
        f"Security:              "
        f"{result['ticker']}"
    )


    print(
        f"Action:                "
        f"{result['action']}"
    )


    print(
        f"Quantity:              "
        f"{result['quantity']:,.0f}"
    )


    # ========================================================
    # DECLINE
    # ============================================================

    if (
        result[
            "decision"
        ]
        ==
        "DECLINE"
    ):

        print(
            ">>>>> DECLINE <<<<<"
        )


        print(
            f"Reason: "
            f"{result['reason']}"
        )


        print(
            "=" * 70
        )

        return


    print(
        f"Expected Unwind VWAP:  "
        f"${result['unwind_vwap']:.4f}"
    )


    print(
        f"Visible Coverage:      "
        f"{result['coverage_ratio']:.1%}"
    )


    print(
        f"Volatility:            "
        f"${result['volatility']:.5f}"
    )


    print(
        f"Transaction Cost:      "
        f"${result['transaction_cost']:,.2f}"
    )


    print(
        f"Inventory Risk:        "
        f"${result['inventory_risk']:,.2f}"
    )


    print(
        f"Liquidation Risk:      "
        f"${result['liquidation_risk']:,.2f}"
    )


    print(
        f"Required Profit:       "
        f"${result['required_profit']:,.2f}"
    )


    print(
        "-" * 70
    )


    print(
        f"Auction Price:         "
        f"${result['auction_price']:.2f}"
    )


    print(
        f"Expected Profit:       "
        f"${result['expected_profit']:,.2f}"
    )


    print(
        f"Expected Profit/Share: "
        f"${result['expected_profit_per_share']:.4f}"
    )


    print(
        f">>>>> SUBMIT "
        f"${result['auction_price']:.2f} "
        f"<<<<<"
    )


    print(
        "=" * 70
    )


# ============================================================
# PROCESS FIXED TENDER
# ============================================================

def process_fixed_tender(tender, securities, current_tick, processed_tenders):
    """Fixed tenders only accept or decline; never create trading orders."""
    result = tender_engine.evaluate_tender(tender, securities)
    tender_engine.print_tender_result(result)
    tender_id = tender["tender_id"]
    action = str(tender.get("action", "")).upper()
    qty = int(tender.get("quantity", 0))
    accept = result.get("decision") == "ACCEPT" and action in ("BUY", "SELL") and qty > 0
    if accept and not fits_rit_limits(tender, processed_tenders):
        return
    if DRY_RUN:
        print(f"[DRY RUN] FIXED TENDER {tender_id}: {'ACCEPT' if accept else 'DECLINE'}")
    elif accept:
        accept_tender(tender_id)
        print(f"FIXED TENDER {tender_id}: ACCEPTED; no unwind")
    else:
        decline_tender(tender_id)
        print(f"FIXED TENDER {tender_id}: DECLINED")
    processed_tenders.add(tender_id)

def submit_auction_bid(
    tender,
    result,
    current_tick,
    expiry_tick,
    processed_tenders
):
    """
    IMPORTANT:

    An auction bid is NOT assumed to have won.

    We create PENDING_AUCTION solely to observe the auction outcome.
    """

    global PENDING_AUCTION


    tender_id = (
        tender[
            "tender_id"
        ]
    )


    base = (
        get_base_ticker(
            tender[
                "ticker"
            ]
        )
    )


    action = str(
        tender[
            "action"
        ]
    ).upper()


    quantity = int(
        tender[
            "quantity"
        ]
    )


    position_before = (
        get_live_base_position(
            base
        )
    )


    if action == "BUY":

        expected_after = (
            position_before
            +
            quantity
        )


    elif action == "SELL":

        expected_after = (
            position_before
            -
            quantity
        )


    else:

        raise ValueError(
            f"Unknown auction action: "
            f"{action}"
        )


    # ========================================================
    # CREATE AUCTION LOCK BEFORE SUBMISSION
    # ============================================================

    PENDING_AUCTION = {

        "tender_id":
            tender_id,

        "base":
            base,

        "tender":
            tender,

        "action":
            action,

        "quantity":
            quantity,

        "price":
            result[
                "auction_price"
            ],

        "position_before":
            position_before,

        "expected_after":
            expected_after,

        "submitted_tick":
            current_tick,

        "expiry_tick":
            expiry_tick
    }


    try:

        if not DRY_RUN:

            accept_tender(

                tender_id,

                price=
                    result[
                        "auction_price"
                    ]
            )


    except Exception:

        PENDING_AUCTION = None

        raise


    processed_tenders.add(
        tender_id
    )


    print(
        "\n"
        + "=" * 70
    )


    print(
        "AUCTION BID SUBMITTED - WAITING FOR RESULT"
    )


    print(
        f"Security:        "
        f"{base}"
    )


    print(
        f"Action:          "
        f"{action}"
    )


    print(
        f"Quantity:        "
        f"{quantity:,}"
    )


    print(
        f"Bid Price:       "
        f"${result['auction_price']:.2f}"
    )


    print(
        f"Position before: "
        f"{position_before:+,}"
    )


    print(
        "Arbitrage:       LOCKED"
    )


    print(
        "=" * 70
    )


# ============================================================
# PROCESS PENDING AUCTION RESULT
# ============================================================

def process_pending_auction(current_tick):
    """Observe auction result without modifying holdings."""
    global PENDING_AUCTION
    if PENDING_AUCTION is None:
        return
    state = PENDING_AUCTION
    base = state["base"]
    current = get_live_base_position(base)
    previous = state["position_before"]
    if current != previous:
        print(f"AUCTION {state['tender_id']}: inventory delta {current-previous:+,}; NO UNWIND")
        PENDING_AUCTION = None
    elif current_tick > state["expiry_tick"] + AUCTION_RESULT_GRACE_TICKS:
        print(f"AUCTION {state['tender_id']}: no inventory change by deadline")
        PENDING_AUCTION = None

def process_auction(
    tender,
    securities,
    current_tick,
    processed_tenders
):

    tender_id = (
        tender[
            "tender_id"
        ]
    )


    expiry_tick = (
        get_auction_expiry_tick(
            tender
        )
    )


    if expiry_tick is None:

        print(
            f"\nAUCTION {tender_id} | "
            f"cannot read expiry | "
            f"fields: {list(tender.keys())}"
        )

        return "WAIT"


    ticks_remaining = (

        expiry_tick

        -

        current_tick
    )


    # ========================================================
    # TOO EARLY
    # ============================================================

    if (
        ticks_remaining
        >
        AUCTION_SUBMIT_TICKS_BEFORE_EXPIRY
    ):

        print(
            f"\rAUCTION {tender_id} | "
            f"WAIT | "
            f"{ticks_remaining} ticks remaining",
            end=""
        )

        return "WAIT"


    # ========================================================
    # EXACT SUBMISSION TICK
    # ============================================================

    if (
        ticks_remaining
        ==
        AUCTION_SUBMIT_TICKS_BEFORE_EXPIRY
    ):

        latest_securities = (
            get_securities()
        )


        result = (
            evaluate_auction(

                tender,

                latest_securities
            )
        )


        print_auction_result(
            result
        )


        # ====================================================
        # DECLINE / DO NOT BID
        # ====================================================

        if (
            result[
                "decision"
            ]
            !=
            "SUBMIT"
        ):

            if not DRY_RUN:

                decline_tender(
                    tender_id
                )


            processed_tenders.add(
                tender_id
            )

            return "DONE"


        # ====================================================
        # RIT GROSS / NET HEADROOM
        # ====================================================

        if not fits_rit_limits(
            tender,
            processed_tenders
        ):

            return "DONE"


        # ====================================================
        # SUBMIT AUCTION
        # ====================================================

        submit_auction_bid(

            tender,

            result,

            current_tick,

            expiry_tick,

            processed_tenders
        )

        return "DONE"


    # ========================================================
    # MISSED WINDOW
    # ============================================================

    print(
        f"\nAUCTION {tender_id} | "
        f"submission window missed"
    )


    processed_tenders.add(
        tender_id
    )

    return "DONE"


# ============================================================
# PROCESS NEW TENDERS
# ============================================================

def process_tenders(processed_tenders, securities, current_tick):
    """Process an eligible tender without ever touching market inventory."""
    for tender in get_tenders():
        tid = tender["tender_id"]
        if tid in processed_tenders:
            continue
        try:
            if tender.get("is_fixed_bid", True):
                process_fixed_tender(tender, securities, current_tick, processed_tenders)
                return
            if PENDING_AUCTION is not None:
                continue
            status = process_auction(tender, securities, current_tick, processed_tenders)
            if status != "WAIT":
                return
        except Exception as exc:
            print(f"Tender {tid} error: {exc}")
            return

def process_arbitrage(securities):
    """Only cross-venue MARKET/MARKET arbitrage via iteration_5."""
    trading_engine.process_arbitrage(securities)

def run_iteration_6(config):
    reset_runtime_state()
    configure_round_6_engines()
    print("=" * 70)
    print("ITERATION 6: TENDER ACCEPT/DECLINE + AUCTION BIDS + PURE ARBITRAGE")
    print("No tender unwind, no residual flatten, no directional hedge")
    print("=" * 70)
    processed_tenders = set()
    while True:
        try:
            case_info = get_case()
            if get_round_number(case_info) != config["iteration"]:
                print("Iteration 6 ended.")
                return
            if case_info.get("status") != "ACTIVE":
                return
            tick = int(case_info.get("tick", 0))
            securities = get_securities()
            tender_engine.update_market_history(securities, tick)
            if trading_engine.check_kill_switch():
                time.sleep(0.25)
                continue
            if PENDING_AUCTION is not None:
                process_pending_auction(tick)
            process_tenders(processed_tenders, securities, tick)
            # These are the only market orders allowed in this strategy.
            process_arbitrage(get_securities())
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("Iteration 6 stopped by user.")
            return
        except Exception as exc:
            print(f"Iteration 6 error: {exc}")
            time.sleep(POLL_INTERVAL)

