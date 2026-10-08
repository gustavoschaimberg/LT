# ============================================================
# ITERATION 4
# CUSTOM LT4 PARAMETERS - STRICT TENDER FILTER
# ============================================================
#
# CRZY:
#   Start Price = $15
#   Volatility = 10%
#   Medium Liquidity
#
# TAME:
#   Start Price = $30
#   Volatility = 2%
#   Low Liquidity
#
# Main + Alternative markets
#
# Tender evaluation: automatic
# Tender accept / decline: automatic
# Position unwind: MANUAL
#
# ROUND 4 STRICT FILTERS:
#
#   CRZY:
#       Minimum visible coverage = 50%
#       Minimum risk-adjusted profit = $0.05/share
#
#   TAME:
#       Minimum visible coverage = 60%
#       Minimum risk-adjusted profit = $0.04/share
#
#   Inventory lambda   = 0.004
#   Liquidation lambda = 0.65
#
# Portions of this code were developed with assistance from
# OpenAI ChatGPT (2026).
#
# ============================================================

import time

from api import (
    get_case,
    get_round_number,
    get_tenders,
    get_securities,
    accept_tender,
    decline_tender
)

import strategies.iteration_2 as engine


# ============================================================
# SETTINGS
# ============================================================

DRY_RUN = False

POLL_INTERVAL = 0.20


# ============================================================
# ROUND 4 MARKET PARAMETERS
# ============================================================

ITERATION_PARAMETERS = {

    "CRZY": {
        "start_price": 15.00,
        "volatility": 0.10,
        "liquidity": "MEDIUM"
    },

    "TAME": {
        "start_price": 30.00,
        "volatility": 0.02,
        "liquidity": "LOW"
    }
}


# ============================================================
# ROUND 4 FEES
# ============================================================

MAIN_FEE = 0.02

ALTERNATIVE_FEE = 0.01

ALTERNATIVE_REBATE = 0.005


# ============================================================
# ROUND 4 STRICT RISK PARAMETERS
# ============================================================

ROUND4_LAMBDA_INVENTORY = 0.004

ROUND4_LAMBDA_LIQUIDATION = 0.65

ROUND4_BASE_MIN_VISIBLE_COVERAGE = 0.50

MIN_EXPECTED_PROFIT = 0.0


# ============================================================
# SECURITY-SPECIFIC PROFIT HURDLES
# ============================================================

MIN_PROFIT_PER_SHARE = {

    "CRZY": 0.05,

    "TAME": 0.04
}


# ============================================================
# SECURITY-SPECIFIC COVERAGE HURDLES
# ============================================================

MIN_COVERAGE_BY_SECURITY = {

    "CRZY": 0.50,

    "TAME": 0.60
}


# ============================================================
# POSITION LIMITS
# ============================================================

NET_POSITION_LIMIT = 100_000

GROSS_POSITION_LIMIT = 200_000


# ============================================================
# FINES
# ============================================================

FINE_A = 0.01

FINE_B = 10_000

FINE_C = 0.02

FINE_D = 1.00


# ============================================================
# CONFIGURE ROUND 4 ENGINE
# ============================================================

def configure_round_4_engine():
    """
    Applies Round 4 parameters only when Round 4 starts.

    This prevents Round 4's stricter parameters from changing
    Iterations 2 and 3 simply because this module was imported.
    """

    # --------------------------------------------------------
    # FEES
    # --------------------------------------------------------

    engine.MAIN_FEE = (
        MAIN_FEE
    )

    engine.ALTERNATIVE_FEE = (
        ALTERNATIVE_FEE
    )

    engine.ALTERNATIVE_REBATE = (
        ALTERNATIVE_REBATE
    )


    # --------------------------------------------------------
    # RISK
    # --------------------------------------------------------

    engine.LAMBDA_INVENTORY = (
        ROUND4_LAMBDA_INVENTORY
    )

    engine.LAMBDA_LIQUIDATION = (
        ROUND4_LAMBDA_LIQUIDATION
    )

    engine.MIN_VISIBLE_COVERAGE = (
        ROUND4_BASE_MIN_VISIBLE_COVERAGE
    )

    engine.MIN_EXPECTED_PROFIT = (
        MIN_EXPECTED_PROFIT
    )


    # --------------------------------------------------------
    # POSITION LIMITS
    # --------------------------------------------------------

    engine.NET_POSITION_LIMIT = (
        NET_POSITION_LIMIT
    )

    engine.GROSS_POSITION_LIMIT = (
        GROSS_POSITION_LIMIT
    )


# ============================================================
# ROUND 4 TENDER EVALUATION
# ============================================================

def evaluate_round_4_tender(
    tender,
    securities
):
    """
    First runs the standard Iteration 2 risk-adjusted model.

    If that model accepts the tender, Round 4 then applies:

        1. Security-specific visible coverage hurdle

        2. Security-specific minimum expected
           profit per share hurdle
    """

    # ========================================================
    # BASE MODEL
    # ========================================================

    result = (
        engine.evaluate_tender(

            tender,

            securities
        )
    )


    # ========================================================
    # BASE MODEL ALREADY DECLINED
    # ========================================================

    if (
        result.get(
            "decision"
        )
        !=
        "ACCEPT"
    ):

        return result


    # ========================================================
    # SECURITY
    # ========================================================

    base = str(
        result[
            "ticker"
        ]
    ).split(
        "_"
    )[0]


    quantity = float(
        result[
            "quantity"
        ]
    )


    expected_profit = float(
        result[
            "expected_profit"
        ]
    )


    coverage_ratio = float(
        result.get(
            "coverage_ratio",
            0.0
        )
    )


    # ========================================================
    # SECURITY-SPECIFIC COVERAGE REQUIREMENT
    # ========================================================

    required_coverage = (
        MIN_COVERAGE_BY_SECURITY.get(

            base,

            ROUND4_BASE_MIN_VISIBLE_COVERAGE
        )
    )


    result[
        "required_coverage"
    ] = required_coverage


    if (
        coverage_ratio
        <
        required_coverage
    ):

        result[
            "decision"
        ] = "DECLINE"


        result[
            "reason"
        ] = (

            f"Round 4 coverage hurdle not met: "
            f"{coverage_ratio:.1%} "
            f"< "
            f"{required_coverage:.1%}"
        )


        return result


    # ========================================================
    # EXPECTED PROFIT PER SHARE
    # ========================================================

    if quantity <= 0:

        result[
            "decision"
        ] = "DECLINE"


        result[
            "reason"
        ] = (
            "Invalid tender quantity"
        )


        return result


    expected_profit_per_share = (

        expected_profit

        /

        quantity
    )


    required_profit_per_share = (
        MIN_PROFIT_PER_SHARE.get(

            base,

            0.05
        )
    )


    result[
        "expected_profit_per_share"
    ] = expected_profit_per_share


    result[
        "required_profit_per_share"
    ] = required_profit_per_share


    # ========================================================
    # PROFIT HURDLE
    # ========================================================

    if (
        expected_profit_per_share
        <
        required_profit_per_share
    ):

        result[
            "decision"
        ] = "DECLINE"


        result[
            "reason"
        ] = (

            f"Round 4 profit hurdle not met: "
            f"${expected_profit_per_share:.4f}/share "
            f"< "
            f"${required_profit_per_share:.4f}/share"
        )


        return result


    # ========================================================
    # PASSED ALL ROUND 4 FILTERS
    # ========================================================

    result[
        "decision"
    ] = "ACCEPT"


    result[
        "reason"
    ] = (

        f"Round 4 hurdles passed | "
        f"Profit "
        f"${expected_profit_per_share:.4f}/share "
        f">= "
        f"${required_profit_per_share:.4f}/share | "
        f"Coverage "
        f"{coverage_ratio:.1%} "
        f">= "
        f"{required_coverage:.1%}"
    )


    return result


# ============================================================
# PRINT ROUND 4 RESULT
# ============================================================

def print_round_4_result(
    result
):

    # First print standard Iteration 2 analysis.

    engine.print_tender_result(
        result
    )


    # ========================================================
    # ADD ROUND 4 HURDLE INFORMATION
    # ========================================================

    if (
        result.get(
            "expected_profit"
        )
        is None
    ):

        return


    quantity = float(
        result.get(
            "quantity",
            0
        )
    )


    if quantity <= 0:

        return


    expected_profit = float(
        result.get(
            "expected_profit",
            0
        )
    )


    expected_profit_per_share = (

        expected_profit

        /

        quantity
    )


    base = str(
        result[
            "ticker"
        ]
    ).split(
        "_"
    )[0]


    required_profit = (
        MIN_PROFIT_PER_SHARE.get(
            base,
            0.05
        )
    )


    required_coverage = (
        MIN_COVERAGE_BY_SECURITY.get(
            base,
            0.50
        )
    )


    coverage = float(
        result.get(
            "coverage_ratio",
            0
        )
    )


    print(
        "ROUND 4 STRICT FILTER"
    )


    print(
        f"Expected Profit/Share: "
        f"${expected_profit_per_share:.4f}"
    )


    print(
        f"Required Profit/Share: "
        f"${required_profit:.4f}"
    )


    print(
        f"Visible Coverage:      "
        f"{coverage:.1%}"
    )


    print(
        f"Required Coverage:     "
        f"{required_coverage:.1%}"
    )


    print(
        f"Final Decision:        "
        f"{result['decision']}"
    )


    print(
        f"Reason:                "
        f"{result['reason']}"
    )


    print(
        "=" * 70
    )


# ============================================================
# DISPLAY PARAMETERS
# ============================================================

def print_iteration_parameters():

    print()

    print(
        "-" * 70
    )


    print(
        "CRZY | Start $15 | "
        "Vol 10% | Medium Liquidity"
    )


    print(
        "TAME | Start $30 | "
        "Vol 2% | Low Liquidity"
    )


    print()

    print(
        "STRICT ROUND 4 FILTERS:"
    )


    print(
        f"CRZY Minimum Coverage:      "
        f"{MIN_COVERAGE_BY_SECURITY['CRZY']:.0%}"
    )


    print(
        f"CRZY Minimum Profit/Share:  "
        f"${MIN_PROFIT_PER_SHARE['CRZY']:.2f}"
    )


    print(
        f"TAME Minimum Coverage:      "
        f"{MIN_COVERAGE_BY_SECURITY['TAME']:.0%}"
    )


    print(
        f"TAME Minimum Profit/Share:  "
        f"${MIN_PROFIT_PER_SHARE['TAME']:.2f}"
    )


    print()


    print(
        f"Inventory Lambda:           "
        f"{ROUND4_LAMBDA_INVENTORY}"
    )


    print(
        f"Liquidation Lambda:         "
        f"{ROUND4_LAMBDA_LIQUIDATION}"
    )


    print()


    print(
        "Fines: "
        "(A, B, C, D) = "
        "($0.01/share, "
        "10,000 shares, "
        "$0.02/share, "
        "$1.00/share)"
    )


    print(
        "-" * 70
    )

    print()


# ============================================================
# ITERATION 4 MAIN LOOP
# ============================================================

def run_iteration_4(
    config
):

    # ========================================================
    # APPLY ROUND 4 PARAMETERS ONLY NOW
    # ========================================================

    configure_round_4_engine()


    print(
        "=" * 70
    )


    print(
        "ITERATION 4"
    )


    print(
        "CUSTOM LT4 | STRICT TENDER FILTER"
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
        "Position Unwind:     MANUAL"
    )


    print(
        "Round 4 Filter:      STRICT"
    )


    print(
        "=" * 70
    )


    print_iteration_parameters()


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
            # STATUS
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

            engine.update_market_history(

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


                # --------------------------------------------
                # ALREADY PROCESSED
                # --------------------------------------------

                if (
                    tender_id
                    in
                    processed_tenders
                ):

                    continue


                # --------------------------------------------
                # ROUND 4 EXPECTS FIXED TENDERS
                # --------------------------------------------

                if (
                    tender.get(
                        "is_fixed_bid",
                        True
                    )
                    is False
                ):

                    print(
                        f"\nUnexpected non-fixed tender "
                        f"{tender_id} in Round 4."
                    )

                    continue


                try:

                    # =========================================
                    # STRICT ROUND 4 EVALUATION
                    # =========================================

                    result = (
                        evaluate_round_4_tender(

                            tender,

                            securities
                        )
                    )


                    # =========================================
                    # DISPLAY
                    # =========================================

                    print_round_4_result(
                        result
                    )


                    # =========================================
                    # ACCEPT
                    # =========================================

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


                        if DRY_RUN:

                            print(
                                "\nDRY RUN: "
                                "Tender would be ACCEPTED."
                            )


                        else:

                            print(
                                "\nTender accepted."
                            )


                        print(
                            ">>> MANUALLY UNWIND USING "
                            "MAIN + ALTERNATIVE <<<"
                        )


                    # =========================================
                    # DECLINE
                    # =========================================

                    else:

                        if not DRY_RUN:

                            decline_tender(
                                tender_id
                            )


                        if DRY_RUN:

                            print(
                                "\nDRY RUN: "
                                "Tender would be DECLINED."
                            )


                        else:

                            print(
                                "\nTender declined."
                            )


                    # =========================================
                    # MARK PROCESSED
                    # =========================================

                    processed_tenders.add(
                        tender_id
                    )


                    # =========================================
                    # REFRESH POSITIONS
                    # =========================================

                    securities = (
                        get_securities()
                    )


                except Exception as error:

                    print(
                        f"\nTender "
                        f"{tender_id} error: "
                        f"{error}"
                    )


            # =================================================
            # NEXT LOOP
            # =================================================

            time.sleep(
                POLL_INTERVAL
            )


        # ====================================================
        # CTRL + C
        # ====================================================

        except KeyboardInterrupt:

            print(
                "\nIteration 4 stopped by user."
            )

            return


        # ====================================================
        # ERROR HANDLING
        # ====================================================

        except Exception as error:

            print(
                f"\nIteration 4 error: "
                f"{error}"
            )


            time.sleep(
                POLL_INTERVAL
            )