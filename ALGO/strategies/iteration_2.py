# ============================================================
# ITERATION 2
# LT4 BASE CASE - 80% SPEED
# ============================================================
#
# Main + Alternative markets.
#
# Tender evaluation: automatic
# Tender accept / decline: automatic
# Position unwind: MANUAL
#
# Tender model:
#
#   Main + Alternative merged book
#   Fee-adjusted routing
#   Minimum visible coverage = 25%
#   lambda_inventory = 0.002
#   lambda_liquidation = 0.35
#
# Automatically returns control to main.py when
# the case name changes from "Round 2".
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
# FEES
# ============================================================

MAIN_FEE = 0.02

ALTERNATIVE_FEE = 0.01

ALTERNATIVE_REBATE = 0.005


# ============================================================
# RISK PARAMETERS
# ============================================================

LAMBDA_INVENTORY = 0.002

LAMBDA_LIQUIDATION = 0.35

MIN_EXPECTED_PROFIT = 0.0


# At least 25% of the tender must currently be visible
# across Main + Alternative.

MIN_VISIBLE_COVERAGE = 0.25


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
# BASIC HELPERS
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
# MARKET TICKERS
# ============================================================

def resolve_market_tickers(
    tender_ticker,
    securities
):

    base = get_base_ticker(
        tender_ticker
    )


    available = {

        security.get("ticker")

        for security in securities
    }


    main_ticker = (
        f"{base}_M"
    )

    alternative_ticker = (
        f"{base}_A"
    )


    if main_ticker not in available:

        raise ValueError(
            f"Main ticker not found: "
            f"{main_ticker}"
        )


    if alternative_ticker not in available:

        raise ValueError(
            f"Alternative ticker not found: "
            f"{alternative_ticker}"
        )


    return (
        main_ticker,
        alternative_ticker
    )


# ============================================================
# POSITIONS
# ============================================================

def get_total_position(
    base,
    securities
):

    total = 0


    for security in securities:

        ticker = security.get(
            "ticker",
            ""
        )


        if (
            get_base_ticker(ticker)
            ==
            base
        ):

            total += int(
                security.get(
                    "position",
                    0
                )
            )


    return total


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
# POSITION LIMITS
# ============================================================

def check_position_limits(
    positions,
    base,
    projected_position
):

    projected = (
        positions.copy()
    )


    projected[
        base
    ] = projected_position


    net_position = sum(
        projected.values()
    )


    gross_position = sum(

        abs(position)

        for position
        in projected.values()
    )


    valid = (

        abs(net_position)
        <= NET_POSITION_LIMIT

        and

        gross_position
        <= GROSS_POSITION_LIMIT
    )


    return (
        valid,
        net_position,
        gross_position
    )


# ============================================================
# MERGED MAIN + ALTERNATIVE BOOK
# ============================================================

def build_combined_book(
    main_book,
    alternative_book,
    tender_action
):
    """
    Builds ONE executable book from both venues.

    BUY tender:
        We become LONG.
        Need to SELL into bids.

        Effective proceeds:
            bid - fee

        Highest effective bid first.

    SELL tender:
        We become SHORT.
        Need to BUY from asks.

        Effective cost:
            ask + fee

        Lowest effective ask first.
    """

    action = (
        tender_action.upper()
    )


    if action == "BUY":

        side = "bids"


    elif action == "SELL":

        side = "asks"


    else:

        raise ValueError(
            f"Unknown tender action: "
            f"{action}"
        )


    combined = []


    # ========================================================
    # MAIN
    # ========================================================

    for order in main_book.get(
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


        price = float(
            order["price"]
        )


        if action == "BUY":

            effective_price = (
                price
                -
                MAIN_FEE
            )


        else:

            effective_price = (
                price
                +
                MAIN_FEE
            )


        combined.append({

            "venue":
                "main",

            "price":
                price,

            "effective_price":
                effective_price,

            "quantity":
                quantity
        })


    # ========================================================
    # ALTERNATIVE
    # ========================================================

    for order in alternative_book.get(
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


        price = float(
            order["price"]
        )


        if action == "BUY":

            effective_price = (
                price
                -
                ALTERNATIVE_FEE
            )


        else:

            effective_price = (
                price
                +
                ALTERNATIVE_FEE
            )


        combined.append({

            "venue":
                "alternative",

            "price":
                price,

            "effective_price":
                effective_price,

            "quantity":
                quantity
        })


    # ========================================================
    # SORT BOTH VENUES TOGETHER
    # ========================================================

    if action == "BUY":

        # Sell inventory:
        # highest net proceeds first.

        combined.sort(

            key=lambda x:
                x["effective_price"],

            reverse=True
        )


    else:

        # Buy inventory:
        # lowest all-in cost first.

        combined.sort(

            key=lambda x:
                x["effective_price"]
        )


    return combined


# ============================================================
# EXECUTABLE VWAP
# ============================================================

def calculate_executable_vwap(
    main_book,
    alternative_book,
    tender_action,
    tender_quantity
):
    """
    Evaluates current executable liquidity across BOTH books.

    We no longer require 100% of the tender quantity to be
    visible at that exact moment.

    At least 25% must be visible.

    The currently visible VWAP is then used as the expected
    unwind price, while missing liquidity is penalized through
    Liquidation Risk.
    """

    combined_book = (
        build_combined_book(

            main_book,

            alternative_book,

            tender_action
        )
    )


    # ========================================================
    # COMBINED DEPTH
    # ========================================================

    visible_depth = sum(

        order["quantity"]

        for order in combined_book
    )


    if tender_quantity <= 0:

        return (
            None,
            [],
            visible_depth
        )


    coverage_ratio = (

        visible_depth

        /

        float(
            tender_quantity
        )
    )


    # ========================================================
    # MINIMUM COVERAGE
    # ========================================================

    if (
        coverage_ratio
        <
        MIN_VISIBLE_COVERAGE
    ):

        return (
            None,
            [],
            visible_depth
        )


    # ========================================================
    # WALK THROUGH MERGED BOOK
    # ========================================================

    remaining = float(
        tender_quantity
    )


    total_value = 0.0

    executions = []


    for order in combined_book:

        if remaining <= 0:

            break


        execution_quantity = min(

            remaining,

            order["quantity"]
        )


        total_value += (

            execution_quantity

            *

            order["price"]
        )


        executions.append({

            "venue":
                order["venue"],

            "price":
                order["price"],

            "effective_price":
                order["effective_price"],

            "quantity":
                execution_quantity
        })


        remaining -= (
            execution_quantity
        )


    # ========================================================
    # QUANTITY OBSERVED
    # ========================================================

    executed_quantity = sum(

        execution["quantity"]

        for execution
        in executions
    )


    if executed_quantity <= 0:

        return (
            None,
            executions,
            visible_depth
        )


    # ========================================================
    # VWAP OF OBSERVED LIQUIDITY
    # ========================================================

    executable_vwap = (

        total_value

        /

        executed_quantity
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
    executions,
    target_quantity=None
):
    """
    Computes observed fees.

    If only part of the tender is currently visible,
    the average observed fee/share is projected over the
    full tender quantity.
    """

    observed_cost = 0.0

    observed_quantity = 0.0


    for execution in executions:

        quantity = (
            execution[
                "quantity"
            ]
        )


        if (
            execution["venue"]
            ==
            "main"
        ):

            fee = MAIN_FEE


        else:

            fee = (
                ALTERNATIVE_FEE
            )


        observed_cost += (

            quantity

            *

            fee
        )


        observed_quantity += (
            quantity
        )


    if observed_quantity <= 0:

        return 0.0


    if target_quantity is None:

        return observed_cost


    average_fee = (

        observed_cost

        /

        observed_quantity
    )


    return (

        average_fee

        *

        target_quantity
    )


# ============================================================
# COMBINED MID PRICE
# ============================================================

def calculate_combined_mid_price(
    main_book,
    alternative_book
):

    bids = []

    asks = []


    for book in [
        main_book,
        alternative_book
    ]:

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


    return (

        max(bids)

        +

        min(asks)

    ) / 2


# ============================================================
# PRICE HISTORY
# ============================================================

def update_price_history(
    base,
    mid_price
):

    if mid_price is None:

        return


    if base not in price_history:

        price_history[
            base
        ] = deque(

            maxlen=
                VOLATILITY_WINDOW
        )


    price_history[
        base
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


    if (
        current_tick
        ==
        last_history_tick
    ):

        return


    last_history_tick = (
        current_tick
    )


    processed = set()


    for security in securities:

        ticker = security.get(
            "ticker",
            ""
        )


        base = get_base_ticker(
            ticker
        )


        if base in processed:

            continue


        processed.add(
            base
        )


        try:

            (
                main_ticker,
                alternative_ticker

            ) = resolve_market_tickers(

                ticker,

                securities
            )


            main_book = get_order_book(

                main_ticker,

                limit=5
            )


            alternative_book = (
                get_order_book(

                    alternative_ticker,

                    limit=5
                )
            )


            mid_price = (
                calculate_combined_mid_price(

                    main_book,

                    alternative_book
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
    base
):

    if base not in price_history:

        return 0.0


    prices = list(
        price_history[
            base
        ]
    )


    if len(prices) < 3:

        return 0.0


    changes = [

        prices[i]

        -

        prices[i - 1]

        for i in range(
            1,
            len(prices)
        )
    ]


    if len(changes) < 2:

        return 0.0


    return statistics.pstdev(
        changes
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


    base = get_base_ticker(
        tender_ticker
    )


    # ========================================================
    # MARKETS
    # ========================================================

    (
        main_ticker,
        alternative_ticker

    ) = resolve_market_tickers(

        tender_ticker,

        securities
    )


    # ========================================================
    # INVENTORY
    # ========================================================

    inventory_before = (
        get_total_position(

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
            f"Unknown action: "
            f"{action}"
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
        valid,
        projected_net,
        projected_gross

    ) = check_position_limits(

        positions,

        base,

        inventory_after
    )


    if not valid:

        return {

            "tender_id":
                tender_id,

            "ticker":
                base,

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
    # BOTH ORDER BOOKS
    # ========================================================

    main_book = get_order_book(

        main_ticker,

        limit=BOOK_LIMIT
    )


    alternative_book = (
        get_order_book(

            alternative_ticker,

            limit=BOOK_LIMIT
        )
    )


    # ========================================================
    # MERGED VWAP
    # ========================================================

    (
        executable_vwap,
        executions,
        visible_depth

    ) = calculate_executable_vwap(

        main_book,

        alternative_book,

        action,

        quantity
    )


    coverage_ratio = (

        visible_depth

        /

        float(quantity)

        if quantity > 0

        else 0.0
    )


    # ========================================================
    # INSUFFICIENT COVERAGE
    # ========================================================

    if executable_vwap is None:

        return {

            "tender_id":
                tender_id,

            "ticker":
                base,

            "action":
                action,

            "quantity":
                quantity,

            "visible_depth":
                visible_depth,

            "coverage_ratio":
                coverage_ratio,

            "decision":
                "DECLINE",

            "reason":
                (
                    "Visible market coverage below "
                    f"{MIN_VISIBLE_COVERAGE:.0%}"
                ),

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
    # TRANSACTION COST
    # ========================================================

    transaction_cost = (
        calculate_transaction_cost(

            executions,

            target_quantity=
                quantity
        )
    )


    # ========================================================
    # INVENTORY RISK
    # ========================================================

    inventory_risk = (
        calculate_inventory_risk(

            inventory_before,

            inventory_after
        )
    )


    # ========================================================
    # VOLATILITY
    # ========================================================

    volatility = (
        calculate_short_term_volatility(
            base
        )
    )


    # ========================================================
    # LIQUIDATION RISK
    # ========================================================

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
            base,

        "main_ticker":
            main_ticker,

        "alternative_ticker":
            alternative_ticker,

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

        "coverage_ratio":
            coverage_ratio,

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
        + "=" * 70
    )


    print(
        f"TENDER "
        f"{result['tender_id']}"
    )


    print(
        "=" * 70
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
            f"Security:        "
            f"{result['ticker']}"
        )


        print(
            f"Action:          "
            f"{result['action']}"
        )


        print(
            f"Quantity:        "
            f"{result['quantity']:,.0f}"
        )


        if "visible_depth" in result:

            print(
                f"Visible Depth:   "
                f"{result['visible_depth']:,.0f}"
            )


        if "coverage_ratio" in result:

            print(
                f"Coverage:        "
                f"{result['coverage_ratio']:.1%}"
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
            "=" * 70
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
        f"Expected Unwind VWAP:  "
        f"${result['vwap']:.4f}"
    )


    print()


    print(
        f"Visible Depth:         "
        f"{result['visible_depth']:,.0f}"
    )


    print(
        f"Visible Coverage:      "
        f"{result['coverage_ratio']:.1%}"
    )


    print(
        f"Volatility:            "
        f"${result['volatility']:.5f}"
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
        "-" * 70
    )


    print(
        f"Expected Profit:       "
        f"${result['expected_profit']:,.2f}"
    )


    print()


    print(
        "Observed Execution Route:"
    )


    for execution in result[
        "executions"
    ]:

        print(

            f"  "
            f"{execution['quantity']:,.0f} "
            f"@ ${execution['price']:.4f} "
            f"[{execution['venue'].upper()}]"
        )


    print()


    print(
        f">>>>> "
        f"{result['decision']} "
        f"<<<<<"
    )


    print(
        "=" * 70
    )


# ============================================================
# ITERATION 2 MAIN LOOP
# ============================================================

def run_iteration_2(config):

    print(
        "=" * 70
    )


    print(
        "ITERATION 2 - LT4 BASE CASE"
    )


    print(
        "80% SPEED | MAIN + ALTERNATIVE"
    )


    print(
        "=" * 70
    )


    print(
        "Mode:                "
        + (
            "DRY RUN"
            if DRY_RUN
            else
            "LIVE"
        )
    )


    print(
        "Tender Evaluation:   AUTOMATIC"
    )


    print(
        "Tender Decision:     AUTOMATIC"
    )


    print(
        "Market Depth:        MAIN + ALTERNATIVE MERGED"
    )


    print(
        "Minimum Coverage:    "
        f"{MIN_VISIBLE_COVERAGE:.0%}"
    )


    print(
        "Inventory Lambda:    "
        f"{LAMBDA_INVENTORY}"
    )


    print(
        "Liquidation Lambda:  "
        f"{LAMBDA_LIQUIDATION}"
    )


    print(
        "Position Unwind:     MANUAL"
    )


    print(
        "=" * 70
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
            # SECURITIES
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
            # CURRENT TENDERS
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


                # =================================================
                # EVALUATE
                # =================================================

                result = (
                    evaluate_tender(

                        tender,

                        securities
                    )
                )


                # =================================================
                # DISPLAY
                # =================================================

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
                        ">>> MANUALLY UNWIND USING "
                        "MAIN + ALTERNATIVE <<<"
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


                # Refresh positions before evaluating another
                # tender in the same cycle.

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
                "\nIteration 2 stopped by user."
            )

            return


        # ====================================================
        # ERROR HANDLING
        # ====================================================

        except Exception as error:

            print(

                f"\nIteration 2 error: "
                f"{error}"
            )


            time.sleep(
                POLL_INTERVAL
            )