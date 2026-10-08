# ============================================================
# ITERATION 3
# LT4 BASE CASE - 100% SPEED
# ============================================================
#
# Main + Alternative markets.
#
# Tender evaluation: automatic
# Tender accept / decline: automatic
# Position unwind: MANUAL
#
# Uses Iteration 2's corrected tender engine:
#
#   - Main + Alternative merged book
#   - Fee-adjusted routing
#   - Minimum visible coverage = 25%
#   - lambda_inventory = 0.002
#   - lambda_liquidation = 0.35
#
# Automatically returns control to main.py when
# the case name changes from "Round 3".
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


# ============================================================
# IMPORT ITERATION 2 ENGINE
# ============================================================

from strategies.iteration_2 import (
    update_market_history,
    evaluate_tender,
    print_tender_result,
    MIN_VISIBLE_COVERAGE,
    LAMBDA_INVENTORY,
    LAMBDA_LIQUIDATION
)


# ============================================================
# SETTINGS
# ============================================================

DRY_RUN = False

# Iteration 3 runs at 100% speed.
POLL_INTERVAL = 0.20


# ============================================================
# ITERATION 3 MAIN LOOP
# ============================================================

def run_iteration_3(config):

    print(
        "=" * 70
    )

    print(
        "ITERATION 3 - LT4 BASE CASE"
    )

    print(
        "100% SPEED | MAIN + ALTERNATIVE"
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


    # ========================================================
    # TENDERS ALREADY PROCESSED
    # ========================================================

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
            #
            # Uses Iteration 2's combined Main + Alternative
            # market history logic.
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
                # ITERATION 3 EXPECTS FIXED TENDERS
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
                        f"{tender_id} in Iteration 3."
                    )

                    continue


                # =================================================
                # EVALUATE TENDER
                # =================================================
                #
                # Calls iteration_2.evaluate_tender().
                #
                # BUY tender:
                #     MAIN BIDS + ALT BIDS
                #
                # SELL tender:
                #     MAIN ASKS + ALT ASKS
                #
                # Both books are merged before calculating:
                #
                #     - visible depth
                #     - executable VWAP
                #     - transaction cost
                #     - inventory risk
                #     - liquidation risk
                #
                # =================================================

                result = (
                    evaluate_tender(

                        tender,

                        securities
                    )
                )


                # =================================================
                # DISPLAY RESULT
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


                # =================================================
                # DECLINE
                # =================================================

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


                # =================================================
                # MARK TENDER PROCESSED
                # =================================================

                processed_tenders.add(
                    tender_id
                )


                # =================================================
                # REFRESH POSITIONS
                # =================================================
                #
                # Important if more than one tender exists in
                # the same polling cycle.
                # =================================================

                securities = (
                    get_securities()
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
                "\nIteration 3 stopped by user."
            )

            return


        # ====================================================
        # ERROR HANDLING
        # ====================================================

        except Exception as error:

            print(

                f"\nIteration 3 error: "
                f"{error}"
            )


            time.sleep(
                POLL_INTERVAL
            )