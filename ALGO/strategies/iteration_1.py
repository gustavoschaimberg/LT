# ============================================================
# ITERATION 1
# LT3 BASE CASE - 80% SPEED
# ============================================================
#
# Main market only.
#
# Tender evaluation: automatic
# Tender accept / decline: automatic
# Position unwind: MANUAL
#
# Automatically returns control to main.py when
# the case name changes from "Round 1".
#
# Portions of this code were developed with assistance from
# OpenAI ChatGPT (2026).
#
# ============================================================

import time
import statistics

from collections import deque

from api import (
    get_case,
    get_round_number,
    get_tenders,
    get_securities,
    get_order_book,
    accept_tender,
    decline_tender
)


# ============================================================
# SETTINGS
# ============================================================

DRY_RUN = False

POLL_INTERVAL = 0.25

BOOK_LIMIT = 100

VOLATILITY_WINDOW = 30


# ============================================================
# CASE PARAMETERS
# ============================================================

MAIN_FEE = 0.02

LAMBDA_INVENTORY = 0.005

LAMBDA_LIQUIDATION = 1.0

MIN_EXPECTED_PROFIT = 0.0


# ============================================================
# POSITION LIMITS
# ============================================================

NET_POSITION_LIMIT = 100_000

GROSS_POSITION_LIMIT = 200_000


# ============================================================
# PRICE HISTORY
# ============================================================

price_history = {}

last_history_tick = None


# ============================================================
# HELPERS
# ============================================================

def get_base_ticker(ticker):

    return ticker.split("_")[0]


def remaining_quantity(order):

    quantity = float(
        order.get(
            "quantity",
            0
        )
    )

    quantity_filled = float(
        order.get(
            "quantity_filled",
            0
        )
    )

    return max(
        quantity - quantity_filled,
        0
    )


# ============================================================
# MAIN MARKET TICKER
# ============================================================

def resolve_main_ticker(
    tender_ticker,
    securities
):

    available_tickers = {

        security.get("ticker")

        for security in securities
    }


    if tender_ticker in available_tickers:

        return tender_ticker


    base = get_base_ticker(
        tender_ticker
    )


    main_ticker = (
        f"{base}_M"
    )


    if main_ticker in available_tickers:

        return main_ticker


    if base in available_tickers:

        return base


    raise ValueError(
        f"Could not find Main ticker for {tender_ticker}"
    )


# ============================================================
# INVENTORY
# ============================================================

def get_total_position(
    base_ticker,
    securities
):

    total_position = 0


    for security in securities:

        ticker = security.get(
            "ticker",
            ""
        )


        if (
            get_base_ticker(ticker)
            ==
            base_ticker
        ):

            total_position += int(
                security.get(
                    "position",
                    0
                )
            )


    return total_position


def get_all_positions(
    securities
):

    positions = {}


    for security in securities:

        ticker = security.get(
            "ticker",
            ""
        )


        base = get_base_ticker(
            ticker
        )


        if base not in positions:

            positions[base] = 0


        positions[base] += int(
            security.get(
                "position",
                0
            )
        )


    return positions


# ============================================================
# POSITION LIMIT CHECK
# ============================================================

def check_position_limits(
    positions,
    base_ticker,
    inventory_after
):

    projected = positions.copy()

    projected[
        base_ticker
    ] = inventory_after


    net_position = sum(
        projected.values()
    )


    gross_position = sum(

        abs(position)

        for position
        in projected.values()
    )


    within_limits = (

        abs(net_position)
        <= NET_POSITION_LIMIT

        and

        gross_position
        <= GROSS_POSITION_LIMIT
    )


    return (
        within_limits,
        net_position,
        gross_position
    )


# ============================================================
# EXECUTABLE VWAP
# ============================================================

def calculate_executable_vwap(
    book,
    tender_action,
    tender_quantity
):

    action = tender_action.upper()


    # BUY tender:
    # we buy from client -> become LONG
    # unwind by SELLING into bids

    if action == "BUY":

        side = "bids"

        reverse_sort = True


    # SELL tender:
    # we sell to client -> become SHORT
    # unwind by BUYING from asks

    elif action == "SELL":

        side = "asks"

        reverse_sort = False


    else:

        raise ValueError(
            f"Unknown tender action: {action}"
        )


    orders = []


    for order in book.get(
        side,
        []
    ):

        quantity = (
            remaining_quantity(
                order
            )
        )


        if quantity <= 0:

            continue


        orders.append({

            "price":
                float(
                    order["price"]
                ),

            "quantity":
                quantity
        })


    orders.sort(

        key=lambda x:
            x["price"],

        reverse=
            reverse_sort
    )


    visible_depth = sum(

        order["quantity"]

        for order in orders
    )


    remaining = float(
        tender_quantity
    )


    total_execution_value = 0.0

    executions = []


    for order in orders:

        if remaining <= 0:

            break


        execution_quantity = min(

            remaining,

            order["quantity"]
        )


        total_execution_value += (

            execution_quantity

            *

            order["price"]
        )


        executions.append({

            "price":
                order["price"],

            "quantity":
                execution_quantity
        })


        remaining -= (
            execution_quantity
        )


    # Iteration 1 keeps the conservative rule:
    # full tender must currently be visible.

    if remaining > 0:

        return (
            None,
            executions,
            visible_depth
        )


    executable_vwap = (

        total_execution_value

        /

        tender_quantity
    )


    return (
        executable_vwap,
        executions,
        visible_depth
    )


# ============================================================
# TRANSACTION COST
# ============================================================

def calculate_transaction_cost(
    quantity
):

    return (

        quantity

        *

        MAIN_FEE
    )


# ============================================================
# MID PRICE
# ============================================================

def calculate_mid_price(
    book
):

    bids = []

    asks = []


    for order in book.get(
        "bids",
        []
    ):

        if (
            remaining_quantity(order)
            >
            0
        ):

            bids.append(
                float(
                    order["price"]
                )
            )


    for order in book.get(
        "asks",
        []
    ):

        if (
            remaining_quantity(order)
            >
            0
        ):

            asks.append(
                float(
                    order["price"]
                )
            )


    if (
        not bids
        or
        not asks
    ):

        return None


    best_bid = max(
        bids
    )


    best_ask = min(
        asks
    )


    return (

        best_bid

        +

        best_ask

    ) / 2


# ============================================================
# PRICE HISTORY
# ============================================================

def update_price_history(
    ticker,
    mid_price
):

    if mid_price is None:

        return


    if ticker not in price_history:

        price_history[
            ticker
        ] = deque(

            maxlen=
                VOLATILITY_WINDOW
        )


    price_history[
        ticker
    ].append(
        mid_price
    )


# ============================================================
# UPDATE MARKET HISTORY
# ============================================================

def update_market_history(
    securities,
    current_tick
):

    global last_history_tick


    # Only one observation per RIT tick

    if (
        current_tick
        ==
        last_history_tick
    ):

        return


    last_history_tick = (
        current_tick
    )


    processed_bases = set()


    for security in securities:

        ticker = security.get(
            "ticker",
            ""
        )


        base = get_base_ticker(
            ticker
        )


        if base in processed_bases:

            continue


        processed_bases.add(
            base
        )


        try:

            main_ticker = (
                resolve_main_ticker(

                    ticker,

                    securities
                )
            )


            book = get_order_book(

                main_ticker,

                limit=5
            )


            mid_price = (
                calculate_mid_price(
                    book
                )
            )


            update_price_history(

                base,

                mid_price
            )


        except Exception:

            continue


# ============================================================
# VOLATILITY
# ============================================================

def calculate_short_term_volatility(
    ticker
):

    if ticker not in price_history:

        return 0.0


    prices = list(
        price_history[
            ticker
        ]
    )


    if len(prices) < 3:

        return 0.0


    price_changes = [

        prices[i]

        -

        prices[i - 1]

        for i in range(
            1,
            len(prices)
        )
    ]


    if len(price_changes) < 2:

        return 0.0


    return statistics.pstdev(
        price_changes
    )


# ============================================================
# INVENTORY RISK
# ============================================================

def calculate_inventory_risk(
    inventory_before,
    inventory_after
):

    inventory_change = (

        abs(inventory_after)

        -

        abs(inventory_before)
    )


    return (

        LAMBDA_INVENTORY

        *

        inventory_change
    )


# ============================================================
# LIQUIDATION RISK
# ============================================================

def calculate_liquidation_risk(
    quantity,
    visible_depth,
    volatility
):

    if visible_depth <= 0:

        return float(
            "inf"
        )


    liquidity_ratio = (

        quantity

        /

        visible_depth
    )


    return (

        LAMBDA_LIQUIDATION

        *

        volatility

        *

        quantity

        *

        liquidity_ratio
    )


# ============================================================
# TENDER EVALUATION
# ============================================================

def evaluate_tender(
    tender,
    securities
):

    tender_id = tender[
        "tender_id"
    ]


    tender_ticker = tender[
        "ticker"
    ]


    action = tender[
        "action"
    ].upper()


    quantity = int(
        tender[
            "quantity"
        ]
    )


    tender_price = float(
        tender[
            "price"
        ]
    )


    base_ticker = (
        get_base_ticker(
            tender_ticker
        )
    )


    main_ticker = (
        resolve_main_ticker(

            tender_ticker,

            securities
        )
    )


    # ========================================================
    # INVENTORY
    # ========================================================

    inventory_before = (
        get_total_position(

            base_ticker,

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
            f"Unknown action: {action}"
        )


    # ========================================================
    # POSITION LIMITS
    # ========================================================

    positions = (
        get_all_positions(
            securities
        )
    )


    (
        within_limits,
        projected_net,
        projected_gross

    ) = check_position_limits(

        positions,

        base_ticker,

        inventory_after
    )


    if not within_limits:

        return {

            "tender_id":
                tender_id,

            "ticker":
                main_ticker,

            "action":
                action,

            "quantity":
                quantity,

            "decision":
                "DECLINE",

            "reason":
                "Position limit violation",

            "expected_profit":
                None
        }


    # ========================================================
    # ORDER BOOK
    # ========================================================

    book = get_order_book(

        main_ticker,

        limit=BOOK_LIMIT
    )


    # ========================================================
    # EXECUTABLE VWAP
    # ========================================================

    (
        executable_vwap,
        executions,
        visible_depth

    ) = calculate_executable_vwap(

        book,

        action,

        quantity
    )


    # ========================================================
    # INSUFFICIENT MARKET DEPTH
    # ========================================================

    if executable_vwap is None:

        return {

            "tender_id":
                tender_id,

            "ticker":
                main_ticker,

            "action":
                action,

            "quantity":
                quantity,

            "visible_depth":
                visible_depth,

            "decision":
                "DECLINE",

            "reason":
                "Insufficient visible market depth",

            "expected_profit":
                None
        }


    # ========================================================
    # EXPECTED UNWIND PROFIT
    # ========================================================

    if action == "BUY":

        expected_unwind_profit = (

            executable_vwap

            -

            tender_price

        ) * quantity


    else:

        expected_unwind_profit = (

            tender_price

            -

            executable_vwap

        ) * quantity


    # ========================================================
    # COSTS / RISKS
    # ========================================================

    transaction_cost = (
        calculate_transaction_cost(
            quantity
        )
    )


    inventory_risk = (
        calculate_inventory_risk(

            inventory_before,

            inventory_after
        )
    )


    volatility = (
        calculate_short_term_volatility(
            base_ticker
        )
    )


    liquidation_risk = (
        calculate_liquidation_risk(

            quantity,

            visible_depth,

            volatility
        )
    )


    # ========================================================
    # EXPECTED PROFIT
    # ========================================================

    expected_profit = (

        expected_unwind_profit

        -

        transaction_cost

        -

        inventory_risk

        -

        liquidation_risk
    )


    # ========================================================
    # DECISION
    # ========================================================

    if (
        expected_profit
        >
        MIN_EXPECTED_PROFIT
    ):

        decision = "ACCEPT"


    else:

        decision = "DECLINE"


    return {

        "tender_id":
            tender_id,

        "ticker":
            main_ticker,

        "action":
            action,

        "quantity":
            quantity,

        "tender_price":
            tender_price,

        "vwap":
            executable_vwap,

        "expected_unwind_profit":
            expected_unwind_profit,

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

        "liquidation_risk":
            liquidation_risk,

        "expected_profit":
            expected_profit,

        "projected_net":
            projected_net,

        "projected_gross":
            projected_gross,

        "executions":
            executions,

        "decision":
            decision,

        "reason":
            "Risk-adjusted expected profitability"
    }


# ============================================================
# PRINT RESULT
# ============================================================

def print_tender_result(
    result
):

    print(
        "\n"
        + "=" * 65
    )


    print(
        f"TENDER "
        f"{result['tender_id']}"
    )


    print(
        "=" * 65
    )


    # ========================================================
    # EARLY DECLINE
    # ========================================================

    if (
        result.get(
            "expected_profit"
        )
        is None
    ):

        print(
            f"Security: "
            f"{result['ticker']}"
        )


        print(
            f"Action:   "
            f"{result['action']}"
        )


        print(
            f"Quantity: "
            f"{result['quantity']:,.0f}"
        )


        if "visible_depth" in result:

            print(
                f"Visible Depth: "
                f"{result['visible_depth']:,.0f}"
            )


        print()


        print(
            f">>>>> "
            f"{result['decision']} "
            f"<<<<<"
        )


        print(
            f"Reason: "
            f"{result['reason']}"
        )


        print(
            "=" * 65
        )


        return


    # ========================================================
    # FULL ANALYSIS
    # ========================================================

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


    print(
        f"Tender Price:          "
        f"${result['tender_price']:.4f}"
    )


    print(
        f"Executable VWAP:       "
        f"${result['vwap']:.4f}"
    )


    print()


    print(
        f"Gross Unwind Profit:   "
        f"${result['expected_unwind_profit']:,.2f}"
    )


    print(
        f"Transaction Cost:     -"
        f"${result['transaction_cost']:,.2f}"
    )


    print(
        f"Inventory Risk:       -"
        f"${result['inventory_risk']:,.2f}"
    )


    print(
        f"Liquidation Risk:     -"
        f"${result['liquidation_risk']:,.2f}"
    )


    print(
        "-" * 65
    )


    print(
        f"Expected Profit:       "
        f"${result['expected_profit']:,.2f}"
    )


    print()


    print(
        f"Inventory Before:      "
        f"{result['inventory_before']:,.0f}"
    )


    print(
        f"Inventory After:       "
        f"{result['inventory_after']:,.0f}"
    )


    print(
        f"Volatility:            "
        f"${result['volatility']:.5f}"
    )


    print(
        f"Visible Depth:         "
        f"{result['visible_depth']:,.0f}"
    )


    print(
        f"Projected Net:         "
        f"{result['projected_net']:,.0f}"
    )


    print(
        f"Projected Gross:       "
        f"{result['projected_gross']:,.0f}"
    )


    print()


    print(
        f">>>>> "
        f"{result['decision']} "
        f"<<<<<"
    )


    print(
        "=" * 65
    )


# ============================================================
# ITERATION 1 MAIN LOOP
# ============================================================

def run_iteration_1(config):

    print(
        "=" * 65
    )


    print(
        "ITERATION 1 - LT3 BASE CASE"
    )


    print(
        "80% SPEED | MAIN MARKET ONLY"
    )


    print(
        "=" * 65
    )


    print(
        "Mode:              "
        + (
            "DRY RUN"
            if DRY_RUN
            else
            "LIVE"
        )
    )


    print(
        "Tender evaluation: AUTOMATIC"
    )


    print(
        "Tender decision:   AUTOMATIC"
    )


    print(
        "Position unwind:   MANUAL"
    )


    print(
        "=" * 65
    )


    processed_tenders = set()


    # ========================================================
    # MAIN LOOP
    # ========================================================

    while True:

        try:

            # ------------------------------------------------
            # CURRENT CASE
            # ------------------------------------------------

            case_info = (
                get_case()
            )


            # ------------------------------------------------
            # CURRENT ROUND
            # ------------------------------------------------

            current_iteration = (
                get_round_number(
                    case_info
                )
            )


            # ------------------------------------------------
            # ROUND CHANGED
            # ------------------------------------------------

            if (
                current_iteration
                !=
                config[
                    "iteration"
                ]
            ):

                print(

                    f"\nIteration "
                    f"{config['iteration']} ended. "

                    f"Switching to "
                    f"Iteration {current_iteration}."
                )


                return


            # ------------------------------------------------
            # CASE STATUS
            # ------------------------------------------------

            status = (
                case_info.get(
                    "status"
                )
            )


            if status != "ACTIVE":

                print(
                    "\nCase is not active. "
                    "Returning to main router."
                )


                return


            # ------------------------------------------------
            # CURRENT TICK
            # ------------------------------------------------

            current_tick = (
                case_info.get(
                    "tick"
                )
            )


            # ------------------------------------------------
            # CURRENT SECURITIES
            # ------------------------------------------------

            securities = (
                get_securities()
            )


            # ------------------------------------------------
            # UPDATE MARKET HISTORY
            # ------------------------------------------------

            update_market_history(

                securities,

                current_tick
            )


            # ------------------------------------------------
            # GET ACTIVE TENDERS
            # ------------------------------------------------

            tenders = (
                get_tenders()
            )


            # =================================================
            # PROCESS NEW TENDERS
            # =================================================

            for tender in tenders:

                tender_id = (
                    tender[
                        "tender_id"
                    ]
                )


                if (
                    tender_id
                    in
                    processed_tenders
                ):

                    continue


                # ------------------------------------------------
                # EVALUATE
                # ------------------------------------------------

                result = (
                    evaluate_tender(

                        tender,

                        securities
                    )
                )


                # ------------------------------------------------
                # DISPLAY
                # ------------------------------------------------

                print_tender_result(
                    result
                )


                # =================================================
                # ACCEPT
                # =================================================

                if (
                    result[
                        "decision"
                    ]
                    ==
                    "ACCEPT"
                ):

                    if not DRY_RUN:

                        accept_tender(
                            tender_id
                        )


                    print(
                        "\nTender accepted."
                    )


                    print(
                        ">>> MANUALLY UNWIND "
                        "THE POSITION <<<"
                    )


                # =================================================
                # DECLINE
                # =================================================

                else:

                    if not DRY_RUN:

                        decline_tender(
                            tender_id
                        )


                    print(
                        "\nTender declined."
                    )


                # ------------------------------------------------
                # MARK PROCESSED
                # ------------------------------------------------

                processed_tenders.add(
                    tender_id
                )


                # Refresh positions if more than one tender
                # appears during the same polling cycle.

                securities = (
                    get_securities()
                )


            # ------------------------------------------------
            # NEXT CHECK
            # ------------------------------------------------

            time.sleep(
                POLL_INTERVAL
            )


        # ====================================================
        # CTRL + C
        # ====================================================

        except KeyboardInterrupt:

            print(
                "\nIteration 1 stopped by user."
            )

            return


        # ====================================================
        # ERRORS
        # ====================================================

        except Exception as error:

            print(

                f"\nIteration 1 error: "
                f"{error}"
            )


            time.sleep(
                POLL_INTERVAL
            )