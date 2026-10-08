# ============================================================
# ITERATION 8
# MAIN MARKET + TIME & SALES + AUCTIONS
# ============================================================
#
# CRZY:
#   Start Price = $12
#   Volatility = 8%
#   High Liquidity
#
# TAME:
#   Start Price = $18
#   Volatility = 11%
#   Low Liquidity
#
# BBSN:
#   Start Price = $85
#   Volatility = 7%
#   Medium Liquidity
#
# Main market only
# API trading enabled
#
# FIXED TENDERS:
#   - automatic evaluation
#   - automatic accept / decline
#
# AUCTIONS:
#   - wait until 1 tick before expiry
#   - use latest order book
#   - use latest Time & Sales
#   - automatic bidding
#
# POSITION UNWIND:
#   - MANUAL ONLY; no inventory trades are generated
#
# BBSN EXTRA PROTECTION:
#   - minimum visible coverage = 40%
#   - minimum profit/share =
#         max($0.08, 10 bps of reference price)
#
# v2 (all securities, fixed tenders and auctions):
#   - risk hurdle from iteration_7: edge per share must beat
#     RISK_Z x sigma_tick x sqrt(T / 3), the dollar drift risk
#     while unwinding. BBSN at $85 and TAME at 11% vol need
#     far more edge than CRZY; the old static BBSN hurdle
#     ($0.10) was below one tick of BBSN drift.
#   - sigma_tick = max(observed, case prior); volatility
#     history reset at round start (BBSN was $45 in round 7)
#   - uses iteration_7 for risk estimates only; no unwind calls
#
# Portions of this code were developed with assistance from
# OpenAI ChatGPT (2026) and Anthropic Claude (2026).
#
# ============================================================

import time
import math
import statistics

from api import (
    get_case,
    get_round_number,
    get_tenders,
    get_securities,
    get_order_book,
    get_time_and_sales,
    accept_tender,
    decline_tender
)

import strategies.iteration_1 as tender_engine
import strategies.iteration_7 as risk_engine


# ============================================================
# SETTINGS
# ============================================================

DRY_RUN = False

POLL_INTERVAL = 0.05

BOOK_LIMIT = 100

TAS_LIMIT = 50


# ============================================================
# ROUND 8 PARAMETERS
# ============================================================

ITERATION_PARAMETERS = {

    "CRZY": {
        "start_price": 12.00,
        "volatility": 0.08,
        "liquidity": "HIGH"
    },

    "TAME": {
        "start_price": 18.00,
        "volatility": 0.11,
        "liquidity": "LOW"
    },

    "BBSN": {
        "start_price": 85.00,
        "volatility": 0.07,
        "liquidity": "MEDIUM"
    }
}


# ============================================================
# FEES
# ============================================================

MAIN_FEE = 0.02


# ============================================================
# GENERAL RISK PARAMETERS
# ============================================================

LAMBDA_INVENTORY = 0.002

LAMBDA_LIQUIDATION = 0.30

MIN_VISIBLE_COVERAGE = 0.20

MIN_EXPECTED_PROFIT = 0.0


# ============================================================
# BBSN EXTRA FILTER
# ============================================================

BBSN_MIN_VISIBLE_COVERAGE = 0.40

BBSN_MIN_PROFIT_PER_SHARE = 0.08

# 12 basis points = 0.12%
BBSN_MIN_PROFIT_BPS = 10


# ============================================================
# AUCTION PARAMETERS
# ============================================================

AUCTION_MIN_PROFIT_PER_SHARE = 0.025

AUCTION_SUBMIT_TICKS_BEFORE_EXPIRY = 1


# ============================================================
# POSITION LIMITS
# ============================================================

NET_POSITION_LIMIT = 100_000

GROSS_POSITION_LIMIT = 200_000


# ============================================================
# CONFIGURE ROUND 8 ENGINES
# ============================================================

def configure_round_8_engines():

    # ========================================================
    # TENDER ENGINE
    # ========================================================

    tender_engine.MAIN_FEE = (
        MAIN_FEE
    )

    tender_engine.LAMBDA_INVENTORY = (
        LAMBDA_INVENTORY
    )

    tender_engine.LAMBDA_LIQUIDATION = (
        LAMBDA_LIQUIDATION
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
    # RISK ESTIMATION ENGINE (NO TRADING)
    # ========================================================

    risk_engine.DRY_RUN = (
        DRY_RUN
    )

    risk_engine.MAIN_FEE = (
        MAIN_FEE
    )

    risk_engine.LAMBDA_INVENTORY = (
        LAMBDA_INVENTORY
    )

    risk_engine.LAMBDA_LIQUIDATION = (
        LAMBDA_LIQUIDATION
    )


# ============================================================
# BASIC HELPERS
# ============================================================

def remaining_quantity(
    order
):

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
        quantity
        -
        quantity_filled,
        0
    )


def get_base_ticker(
    ticker
):

    return str(
        ticker
    ).split("_")[0]


# ============================================================
# BBSN FILTER HELPERS
# ============================================================

def get_required_coverage(
    base
):
    """
    BBSN requires substantially more visible liquidity.

    Other securities use the normal Round 8 threshold.
    """

    if base == "BBSN":

        return (
            BBSN_MIN_VISIBLE_COVERAGE
        )

    return (
        MIN_VISIBLE_COVERAGE
    )


def get_fixed_min_profit_per_share(
    base,
    reference_price
):
    """
    Fixed tenders:

    CRZY / TAME:
        no additional explicit hurdle beyond risk-adjusted
        profitability.

    BBSN:
        max($0.10/share, 12 bps of reference price)
    """

    if base != "BBSN":

        return 0.0


    bps_hurdle = (

        reference_price

        *

        BBSN_MIN_PROFIT_BPS

        /

        10_000
    )


    return max(
        BBSN_MIN_PROFIT_PER_SHARE,
        bps_hurdle
    )


def get_auction_min_profit_per_share(
    base,
    reference_price
):
    """
    Auction hurdle.

    Normal:
        $0.03/share

    BBSN:
        max($0.10/share, 12 bps of reference price)
    """

    if base != "BBSN":

        return (
            AUCTION_MIN_PROFIT_PER_SHARE
        )


    bps_hurdle = (

        reference_price

        *

        BBSN_MIN_PROFIT_BPS

        /

        10_000
    )


    return max(
        BBSN_MIN_PROFIT_PER_SHARE,
        bps_hurdle
    )


# ============================================================
# ROUNDING
# ============================================================

def round_down_cent(
    price
):

    return (
        math.floor(
            price * 100
            +
            1e-9
        )
        /
        100
    )


def round_up_cent(
    price
):

    return (
        math.ceil(
            price * 100
            -
            1e-9
        )
        /
        100
    )


# ============================================================
# MAIN TICKER
# ============================================================

def resolve_main_ticker(
    tender_ticker,
    securities
):

    return (
        tender_engine.resolve_main_ticker(

            tender_ticker,

            securities
        )
    )


# ============================================================
# MAIN MARKET EXPECTED VWAP
# ============================================================

def calculate_main_expected_vwap(
    book,
    tender_action,
    tender_quantity,
    required_coverage
):
    """
    BUY tender:

        We become LONG.
        Unwind = SELL into bids.

    SELL tender:

        We become SHORT.
        Unwind = BUY from asks.

    required_coverage is security-specific.
    """

    action = (
        tender_action.upper()
    )


    # ========================================================
    # BOOK SIDE
    # ============================================================

    if action == "BUY":

        side = "bids"

        reverse_sort = True


    elif action == "SELL":

        side = "asks"

        reverse_sort = False


    else:

        raise ValueError(
            f"Unknown tender action: "
            f"{action}"
        )


    # ========================================================
    # CLEAN ORDERS
    # ============================================================

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
                    order[
                        "price"
                    ]
                ),

            "quantity":
                quantity
        })


    orders.sort(

        key=lambda x:
            x[
                "price"
            ],

        reverse=
            reverse_sort
    )


    # ========================================================
    # VISIBLE DEPTH
    # ============================================================

    visible_depth = sum(

        order[
            "quantity"
        ]

        for order
        in orders
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
    # SECURITY-SPECIFIC COVERAGE FILTER
    # ============================================================

    if (
        coverage_ratio
        <
        required_coverage
    ):

        return (
            None,
            [],
            visible_depth
        )


    # ========================================================
    # WALK BOOK
    # ============================================================

    remaining = float(
        tender_quantity
    )

    total_value = 0.0

    executions = []


    for order in orders:

        if remaining <= 0:

            break


        execution_quantity = min(

            remaining,

            order[
                "quantity"
            ]
        )


        total_value += (

            execution_quantity

            *

            order[
                "price"
            ]
        )


        executions.append({

            "price":
                order[
                    "price"
                ],

            "quantity":
                execution_quantity
        })


        remaining -= (
            execution_quantity
        )


    executed_quantity = sum(

        execution[
            "quantity"
        ]

        for execution
        in executions
    )


    if executed_quantity <= 0:

        return (
            None,
            executions,
            visible_depth
        )


    expected_vwap = (

        total_value

        /

        executed_quantity
    )


    return (
        expected_vwap,
        executions,
        visible_depth
    )


# ============================================================
# TIME & SALES
# ============================================================

def get_tas_data(
    ticker
):

    try:

        data = (
            get_time_and_sales(

                ticker,

                limit=
                    TAS_LIMIT
            )
        )


    except Exception:

        try:

            data = (
                get_time_and_sales(
                    ticker
                )
            )

        except Exception:

            return []


    if not isinstance(
        data,
        list
    ):

        return []


    return data


# ============================================================
# T&S VWAP
# ============================================================

def calculate_tas_vwap(
    ticker
):

    trades = (
        get_tas_data(
            ticker
        )
    )


    if not trades:

        return None


    total_value = 0.0

    total_quantity = 0.0


    for trade in trades:

        try:

            price = float(
                trade[
                    "price"
                ]
            )

            quantity = float(
                trade.get(
                    "quantity",
                    1
                )
            )


        except (
            KeyError,
            TypeError,
            ValueError
        ):

            continue


        if quantity <= 0:

            continue


        total_value += (

            price

            *

            quantity
        )


        total_quantity += (
            quantity
        )


    if total_quantity <= 0:

        return None


    return (

        total_value

        /

        total_quantity
    )


# ============================================================
# T&S VOLATILITY
# ============================================================

def calculate_tas_volatility(
    ticker
):

    trades = (
        get_tas_data(
            ticker
        )
    )


    prices = []


    for trade in trades:

        try:

            prices.append(
                float(
                    trade[
                        "price"
                    ]
                )
            )


        except (
            KeyError,
            TypeError,
            ValueError
        ):

            continue


    if len(
        prices
    ) < 3:

        return 0.0


    price_changes = [

        prices[i]

        -

        prices[
            i - 1
        ]

        for i in range(
            1,
            len(prices)
        )
    ]


    if len(
        price_changes
    ) < 2:

        return 0.0


    return statistics.pstdev(
        price_changes
    )


# ============================================================
# COMBINED VOLATILITY
# ============================================================

def calculate_combined_volatility(
    base,
    main_ticker
):

    book_volatility = (
        tender_engine
        .calculate_short_term_volatility(
            base
        )
    )


    tas_volatility = (
        calculate_tas_volatility(
            main_ticker
        )
    )


    return max(
        book_volatility,
        tas_volatility
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
# FIXED TENDER EVALUATION
# ============================================================

def evaluate_fixed_tender(
    tender,
    securities
):

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


    tender_price = float(
        tender[
            "price"
        ]
    )


    base = (
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
    # SECURITY-SPECIFIC REQUIREMENTS
    # ============================================================

    required_coverage = (
        get_required_coverage(
            base
        )
    )


    required_profit_per_share = (
        get_fixed_min_profit_per_share(

            base,

            tender_price
        )
    )


    # ========================================================
    # INVENTORY
    # ============================================================

    inventory_before = (
        tender_engine
        .get_total_position(

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
    # LIMITS
    # ============================================================

    positions = (
        tender_engine
        .get_all_positions(
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

            "decision":
                "DECLINE",

            "reason":
                "Position limit violation",

            "expected_profit":
                None
        }


    # ========================================================
    # BOOK
    # ============================================================

    book = (
        get_order_book(

            main_ticker,

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

    ) = calculate_main_expected_vwap(

        book,

        action,

        quantity,

        required_coverage
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


    # ========================================================
    # COVERAGE REJECTION
    # ============================================================

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

            "visible_depth":
                visible_depth,

            "coverage_ratio":
                coverage_ratio,

            "required_coverage":
                required_coverage,

            "decision":
                "DECLINE",

            "reason":
                (
                    f"Visible coverage "
                    f"{coverage_ratio:.1%} below "
                    f"{required_coverage:.1%}"
                ),

            "expected_profit":
                None
        }


    # ========================================================
    # GROSS PROFIT
    # ============================================================

    if action == "BUY":

        expected_unwind_profit = (

            unwind_vwap

            -

            tender_price

        ) * quantity


    else:

        expected_unwind_profit = (

            tender_price

            -

            unwind_vwap

        ) * quantity


    # ========================================================
    # COSTS / RISKS
    # ============================================================

    transaction_cost = (

        MAIN_FEE

        *

        quantity
    )


    inventory_risk = (
        tender_engine
        .calculate_inventory_risk(

            inventory_before,

            inventory_after
        )
    )


    volatility_observed = (
        calculate_combined_volatility(

            base,

            main_ticker
        )
    )


    # Floor with the case prior (protects the first ticks).
    volatility = (
        risk_engine.sigma_tick_used(

            base,

            tender_price,

            volatility_observed
        )
    )


    liquidation_risk = (
        tender_engine
        .calculate_liquidation_risk(

            quantity,

            visible_depth,

            volatility
        )
    )


    # ========================================================
    # EXPECTED PROFIT
    # ============================================================

    expected_profit = (

        expected_unwind_profit

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


    # ========================================================
    # DECISION
    # ========================================================
    #
    # Positive expected profit AND per-share edge above
    # max(BBSN static hurdle, dollar-drift risk hurdle).
    #
    # ========================================================

    risk_hurdle, unwind_ticks = risk_engine.risk_hurdle(
        base,
        volatility
    )

    risk_hurdle *= 0.85  # Moderately more aggressive; not a liquidation instruction


    required_profit_per_share = max(
        required_profit_per_share,
        risk_hurdle
    )


    if expected_profit <= MIN_EXPECTED_PROFIT:

        decision = "DECLINE"

        reason = "Expected profit not positive"


    elif expected_profit_per_share < required_profit_per_share:

        decision = "DECLINE"

        reason = (
            f"${expected_profit_per_share:.4f}/share below "
            f"required ${required_profit_per_share:.4f}/share"
        )


    else:

        decision = "ACCEPT"

        reason = (
            f"${expected_profit_per_share:.4f}/share >= "
            f"required ${required_profit_per_share:.4f}/share"
        )


    return {

        "tender_id":
            tender_id,

        "ticker":
            base,

        "main_ticker":
            main_ticker,

        "action":
            action,

        "quantity":
            quantity,

        "tender_price":
            tender_price,

        "vwap":
            unwind_vwap,

        "tas_vwap":
            calculate_tas_vwap(
                main_ticker
            ),

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

        "required_coverage":
            required_coverage,

        "liquidation_risk":
            liquidation_risk,

        "expected_profit":
            expected_profit,

        "expected_profit_per_share":
            expected_profit_per_share,

        "required_profit_per_share":
            required_profit_per_share,

        "projected_net":
            projected_net,

        "projected_gross":
            projected_gross,

        "executions":
            executions,

        "decision":
            decision,

        "reason":
            reason,

        "volatility_observed":
            volatility_observed,

        "volatility_prior":
            risk_engine.sigma_tick_prior(
                base,
                tender_price
            ),

        "risk_hurdle":
            risk_hurdle,

        "unwind_ticks_estimate":
            unwind_ticks
    }


# ============================================================
# PRINT FIXED TENDER RESULT
# ============================================================

def print_fixed_tender_result(
    result
):

    print(
        "\n"
        + "=" * 70
    )


    print(
        f"FIXED TENDER "
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
    # EARLY DECLINE
    # ============================================================

    if (
        result.get(
            "expected_profit"
        )
        is None
    ):

        if "coverage_ratio" in result:

            print(
                f"Visible Coverage:      "
                f"{result['coverage_ratio']:.1%}"
            )


        if "required_coverage" in result:

            print(
                f"Required Coverage:     "
                f"{result['required_coverage']:.1%}"
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
    # FULL RESULT
    # ============================================================

    print(
        f"Tender Price:          "
        f"${result['tender_price']:.4f}"
    )


    print(
        f"Expected Unwind VWAP:  "
        f"${result['vwap']:.4f}"
    )


    if (
        result[
            "tas_vwap"
        ]
        is not None
    ):

        print(
            f"Time & Sales VWAP:     "
            f"${result['tas_vwap']:.4f}"
        )


    print(
        f"Sigma/Tick:            "
        f"observed ${result.get('volatility_observed', result['volatility']):.4f} | "
        f"prior ${result.get('volatility_prior', 0.0):.4f} | "
        f"used ${result['volatility']:.4f}"
    )


    print(
        f"Visible Coverage:      "
        f"{result['coverage_ratio']:.1%}"
    )


    print(
        f"Required Coverage:     "
        f"{result['required_coverage']:.1%}"
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


    print(
        f"Expected Profit/Share: "
        f"${result['expected_profit_per_share']:.4f}"
    )


    print(
        f"Required Profit/Share: "
        f"${result['required_profit_per_share']:.4f} "
        f"(risk hurdle ${result.get('risk_hurdle', 0.0):.4f}, "
        f"~{result.get('unwind_ticks_estimate', '?')} ticks to unwind)"
    )


    print(
        f"Reason:                "
        f"{result['reason']}"
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
# AUCTION EVALUATION
# ============================================================

def evaluate_auction(
    tender,
    securities
):

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


    auction_type = (
        get_auction_description(
            tender
        )
    )


    # ========================================================
    # SECURITY-SPECIFIC COVERAGE
    # ============================================================

    required_coverage = (
        get_required_coverage(
            base
        )
    )


    # ========================================================
    # INVENTORY
    # ============================================================

    inventory_before = (
        tender_engine
        .get_total_position(

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
    # POSITION LIMIT
    # ============================================================

    positions = (
        tender_engine
        .get_all_positions(
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

            "reason":
                "Position limit violation",

            "auction_price":
                None
        }


    # ========================================================
    # LATEST BOOK
    # ============================================================

    book = (
        get_order_book(

            main_ticker,

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

    ) = calculate_main_expected_vwap(

        book,

        action,

        quantity,

        required_coverage
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

            "required_coverage":
                required_coverage,

            "decision":
                "DECLINE",

            "reason":
                (
                    f"Visible coverage "
                    f"{coverage_ratio:.1%} below "
                    f"{required_coverage:.1%}"
                ),

            "auction_price":
                None
        }


    # ========================================================
    # T&S
    # ============================================================

    tas_vwap = (
        calculate_tas_vwap(
            main_ticker
        )
    )


    # ========================================================
    # COSTS / RISKS
    # ============================================================

    transaction_cost = (

        MAIN_FEE

        *

        quantity
    )


    inventory_risk = (
        tender_engine
        .calculate_inventory_risk(

            inventory_before,

            inventory_after
        )
    )


    volatility_observed = (
        calculate_combined_volatility(

            base,

            main_ticker
        )
    )


    # Floor with the case prior (protects the first ticks).
    volatility = (
        risk_engine.sigma_tick_used(

            base,

            unwind_vwap,

            volatility_observed
        )
    )


    liquidation_risk = (
        tender_engine
        .calculate_liquidation_risk(

            quantity,

            visible_depth,

            volatility
        )
    )


    # ========================================================
    # SECURITY-SPECIFIC AUCTION PROFIT HURDLE
    # ============================================================

    risk_hurdle, unwind_ticks = risk_engine.risk_hurdle(
        base,
        volatility
    )

    risk_hurdle *= 0.85  # Moderately more aggressive; not a liquidation instruction


    minimum_profit_per_share = max(

        get_auction_min_profit_per_share(

            base,

            unwind_vwap
        ),

        risk_hurdle
    )


    required_profit = (

        minimum_profit_per_share

        *

        quantity
    )


    # ========================================================
    # TOTAL PRICE ADJUSTMENT
    # ============================================================

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
    # ============================================================

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

                "reason":
                    "Calculated auction price is invalid",

                "auction_price":
                    None
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

        "auction_type":
            auction_type,

        "action":
            action,

        "quantity":
            quantity,

        "unwind_vwap":
            unwind_vwap,

        "tas_vwap":
            tas_vwap,

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

        "required_coverage":
            required_coverage,

        "liquidation_risk":
            liquidation_risk,

        "required_profit":
            required_profit,

        "minimum_profit_per_share":
            minimum_profit_per_share,

        "volatility_observed":
            volatility_observed,

        "volatility_prior":
            risk_engine.sigma_tick_prior(
                base,
                unwind_vwap
            ),

        "risk_hurdle":
            risk_hurdle,

        "unwind_ticks_estimate":
            unwind_ticks,

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
            (
                "Auction price calculated with "
                "security-specific risk hurdle"
            )
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
        f"AUCTION "
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


    if (
        result[
            "decision"
        ]
        ==
        "DECLINE"
    ):

        if "coverage_ratio" in result:

            print(
                f"Visible Coverage:      "
                f"{result['coverage_ratio']:.1%}"
            )


        if "required_coverage" in result:

            print(
                f"Required Coverage:     "
                f"{result['required_coverage']:.1%}"
            )


        print()


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


    if (
        result[
            "tas_vwap"
        ]
        is not None
    ):

        print(
            f"Time & Sales VWAP:     "
            f"${result['tas_vwap']:.4f}"
        )


    print(
        f"Sigma/Tick:            "
        f"observed ${result.get('volatility_observed', result['volatility']):.4f} | "
        f"prior ${result.get('volatility_prior', 0.0):.4f} | "
        f"used ${result['volatility']:.4f}"
    )


    print(
        f"Visible Coverage:      "
        f"{result['coverage_ratio']:.1%}"
    )


    print(
        f"Required Coverage:     "
        f"{result['required_coverage']:.1%}"
    )


    print()


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
        f"Required Profit/Share: "
        f"${result['minimum_profit_per_share']:.4f}"
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


    print()


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

def process_fixed_tender(
    tender,
    securities
):

    result = (
        evaluate_fixed_tender(

            tender,

            securities
        )
    )


    print_fixed_tender_result(
        result
    )


    tender_id = (
        tender[
            "tender_id"
        ]
    )


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
            "\nFixed tender accepted."
        )


        return


    if not DRY_RUN:

        decline_tender(
            tender_id
        )


    print(
        "\nFixed tender declined."
    )


# ============================================================
# PROCESS AUCTION
# ============================================================

def process_auction(
    tender,
    securities,
    current_tick
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
            f"Could not read expiry tick."
        )

        return "WAIT"


    ticks_remaining = (

        expiry_tick

        -

        current_tick
    )


    # ========================================================
    # WAIT
    # ============================================================

    if (
        ticks_remaining
        >
        AUCTION_SUBMIT_TICKS_BEFORE_EXPIRY
    ):

        print(
            f"\rAUCTION {tender_id} | "
            f"WAITING | "
            f"{ticks_remaining} ticks remaining",
            end=""
        )

        return "WAIT"


    # ========================================================
    # FINAL PRICING
    # ============================================================

    if (
        ticks_remaining
        ==
        AUCTION_SUBMIT_TICKS_BEFORE_EXPIRY
    ):

        print(
            f"\n\nAUCTION {tender_id} | "
            f"FINAL PRICING"
        )


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


        if (
            result[
                "decision"
            ]
            ==
            "SUBMIT"
        ):

            if not DRY_RUN:

                accept_tender(

                    tender_id,

                    price=
                        result[
                            "auction_price"
                        ]
                )


            print(
                f"\nAUCTION SUBMITTED | "
                f"${result['auction_price']:.2f}"
            )


            return "PROCESSED"


        if not DRY_RUN:

            decline_tender(
                tender_id
            )


        return "PROCESSED"


    print(
        f"\nAUCTION {tender_id} | "
        f"EXPIRY WINDOW MISSED"
    )


    return "EXPIRED"


# ============================================================
# PROCESS TENDERS
# ============================================================

def process_tenders(
    processed_tenders,
    securities,
    current_tick
):

    tenders = (
        get_tenders()
    )


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


        is_fixed = (
            tender.get(
                "is_fixed_bid",
                True
            )
        )


        try:

            if is_fixed:

                process_fixed_tender(

                    tender,

                    securities
                )


                processed_tenders.add(
                    tender_id
                )


            else:

                auction_status = (
                    process_auction(

                        tender,

                        securities,

                        current_tick
                    )
                )


                if auction_status in {
                    "PROCESSED",
                    "EXPIRED"
                }:

                    processed_tenders.add(
                        tender_id
                    )


        except Exception as error:

            print(
                f"\nTender "
                f"{tender_id} error: "
                f"{error}"
            )


# ============================================================
# DISPLAY PARAMETERS
# ============================================================

def print_round_8_parameters():

    print()

    print(
        "-" * 70
    )


    print(
        "CRZY | Start $12 | "
        "Vol 8%  | High Liquidity"
    )


    print(
        "TAME | Start $18 | "
        "Vol 11% | Low Liquidity"
    )


    print(
        "BBSN | Start $85 | "
        "Vol 7%  | Medium Liquidity"
    )


    print()

    print(
        "BBSN EXTRA FILTER:"
    )


    print(
        f"Coverage minimum:      "
        f"{BBSN_MIN_VISIBLE_COVERAGE:.0%}"
    )


    print(
        f"Absolute hurdle:       "
        f"${BBSN_MIN_PROFIT_PER_SHARE:.2f}/share"
    )


    print(
        f"Relative hurdle:       "
        f"{BBSN_MIN_PROFIT_BPS} bps"
    )


    print()

    risk_engine.print_risk_table()


    print(
        "-" * 70
    )

    print()


# ============================================================
# ITERATION 8 MAIN LOOP
# ============================================================

def run_iteration_8(
    config
):

    configure_round_8_engines()

    # Reset risk-model history; this does NOT execute unwind orders.
    risk_engine.prepare_round(
        ITERATION_PARAMETERS
    )


    print(
        "=" * 70
    )


    print(
        "ITERATION 8"
    )


    print(
        "MAIN MARKET | T&S | AUCTIONS"
    )


    print(
        "=" * 70
    )


    print(
        "Fixed Tenders:       AUTOMATIC"
    )


    print(
        "Auction Bidding:     AUTOMATIC"
    )


    print(
        "Auction Timing:      EXPIRY - 1 TICK"
    )


    print(
        "Position Unwind:     MANUAL ONLY"
    )


    print(
        "Time & Sales:        ENABLED"
    )


    print(
        "BBSN Filter:         MODERATE"
    )


    print(
        "Main Fee:            "
        f"${MAIN_FEE:.3f}/share"
    )


    print(
        "Normal Coverage:     "
        f"{MIN_VISIBLE_COVERAGE:.0%}"
    )


    print(
        "BBSN Coverage:       "
        f"{BBSN_MIN_VISIBLE_COVERAGE:.0%}"
    )


    print(
        "BBSN Min P/S:        "
        f"${BBSN_MIN_PROFIT_PER_SHARE:.2f}"
    )


    print(
        "BBSN Min BPS:        "
        f"{BBSN_MIN_PROFIT_BPS}"
    )


    print(
        "=" * 70
    )


    print_round_8_parameters()


    processed_tenders = set()


    while True:

        try:

            # =================================================
            # CASE
            # ============================================================

            case_info = (
                get_case()
            )


            current_iteration = (
                get_round_number(
                    case_info
                )
            )


            if (
                current_iteration
                !=
                config[
                    "iteration"
                ]
            ):

                return


            if (
                case_info.get(
                    "status"
                )
                !=
                "ACTIVE"
            ):

                return


            current_tick = int(
                case_info.get(
                    "tick",
                    0
                )
            )


            risk_engine.TICKS_PER_PERIOD = int(
                case_info.get("ticks_per_period")
                or risk_engine.TICKS_PER_PERIOD
            )


            # =================================================
            # SECURITIES
            # ============================================================

            securities = (
                get_securities()
            )


            tender_engine.update_market_history(

                securities,

                current_tick
            )


            # =================================================
            # KILL SWITCH (drawdown from peak NLV)
            # ============================================================

            if risk_engine.ex.check_kill_switch():

                time.sleep(
                    0.25
                )

                continue


            # =================================================
            # 1. TENDERS / AUCTIONS
            # ============================================================

            process_tenders(

                processed_tenders,

                securities,

                current_tick
            )


            # =================================================
            # 2. REFRESH
            # ============================================================

            securities = (
                get_securities()
            )


            # =================================================
            # 3. MANUAL UNWIND ONLY (NO ACTION)
            # ============================================================

            # Manual inventory management only. No market order submitted here.
            # The risk model above is for ACCEPT/DECLINE decisions only.
            pass


            # =================================================
            # NEXT LOOP
            # ============================================================

            time.sleep(
                POLL_INTERVAL
            )


        except KeyboardInterrupt:

            print(
                "\nIteration 8 stopped by user."
            )

            return


        except Exception as error:

            print(
                f"\nIteration 8 error: "
                f"{error}"
            )


            time.sleep(
                POLL_INTERVAL
            )