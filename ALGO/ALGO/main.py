# ============================================================
# LT3 / LT4 AUTOMATIC TRADING SYSTEM
# ============================================================
#
# Automatically detects the current evaluation round using:
#
#     case_info["name"]
#
# Example:
#
#     "Round 1" -> Iteration 1
#     "Round 2" -> Iteration 2
#     ...
#     "Round 8" -> Iteration 8
#
# Portions of this code were developed with assistance from
# OpenAI ChatGPT (2026).
#
# ============================================================

import time
import re

from api import get_case

from strategies.iteration_1 import run_iteration_1
from strategies.iteration_2 import run_iteration_2
from strategies.iteration_3 import run_iteration_3
from strategies.iteration_4 import run_iteration_4
from strategies.iteration_5 import run_iteration_5
from strategies.iteration_6 import run_iteration_6
from strategies.iteration_7 import run_iteration_7
from strategies.iteration_8 import run_iteration_8


# ============================================================
# SETTINGS
# ============================================================

CHECK_INTERVAL = 0.25


# ============================================================
# ITERATION CONFIGURATION
# ============================================================

ITERATION_CONFIG = {

    1: {
        "iteration": 1,
        "name": "Round 1",
        "markets": ["main"],
        "api_trading": False,
        "auctions": False
    },

    2: {
        "iteration": 2,
        "name": "Round 2",
        "markets": ["main", "alternative"],
        "api_trading": False,
        "auctions": False
    },

    3: {
        "iteration": 3,
        "name": "Round 3",
        "markets": ["main", "alternative"],
        "api_trading": False,
        "auctions": False
    },

    4: {
        "iteration": 4,
        "name": "Round 4",
        "markets": ["main", "alternative"],
        "api_trading": False,
        "auctions": False
    },

    5: {
        "iteration": 5,
        "name": "Round 5",
        "markets": ["main", "alternative"],
        "api_trading": True,
        "auctions": False
    },

    6: {
        "iteration": 6,
        "name": "Round 6",
        "markets": ["main", "alternative"],
        "api_trading": True,
        "auctions": True
    },

    7: {
        "iteration": 7,
        "name": "Round 7",
        "markets": ["main"],
        "api_trading": True,
        "auctions": False
    },

    8: {
        "iteration": 8,
        "name": "Round 8",
        "markets": ["main"],
        "api_trading": True,
        "auctions": True
    }
}


# ============================================================
# DETECT CURRENT ITERATION
# ============================================================

def detect_iteration(case_info):
    """
    Extracts the evaluation round from the RIT case name.

    Examples:

        "Round 1" -> 1
        "Round 4" -> 4
        "Round 8" -> 8
    """

    case_name = str(
        case_info.get(
            "name",
            ""
        )
    )


    match = re.search(

        r"round\s*(\d+)",

        case_name,

        re.IGNORECASE
    )


    if match is None:

        raise ValueError(

            f"Could not identify iteration "
            f"from case name: {case_name}"
        )


    iteration = int(
        match.group(1)
    )


    if iteration not in ITERATION_CONFIG:

        raise ValueError(

            f"Invalid iteration detected: "
            f"{iteration}"
        )


    return iteration


# ============================================================
# STRATEGY ROUTER
# ============================================================

def run_strategy(
    iteration,
    config
):

    if iteration == 1:

        run_iteration_1(
            config
        )


    elif iteration == 2:

        run_iteration_2(
            config
        )


    elif iteration == 3:

        run_iteration_3(
            config
        )


    elif iteration == 4:

        run_iteration_4(
            config
        )


    elif iteration == 5:

        run_iteration_5(
            config
        )


    elif iteration == 6:

        run_iteration_6(
            config
        )


    elif iteration == 7:

        run_iteration_7(
            config
        )


    elif iteration == 8:

        run_iteration_8(
            config
        )


# ============================================================
# DISPLAY CURRENT ITERATION
# ============================================================

def print_iteration_info(
    iteration,
    config,
    case_info
):

    print(
        "\n"
        + "=" * 70
    )


    print(
        f"AUTOMATICALLY DETECTED "
        f"ITERATION {iteration}"
    )


    print(
        "=" * 70
    )


    print(
        f"Case Name:       "
        f"{case_info.get('name')}"
    )


    print(
        f"Iteration:       "
        f"{iteration}"
    )


    print(
        f"Markets:         "
        f"{', '.join(config['markets'])}"
    )


    print(
        f"API Trading:     "
        f"{config['api_trading']}"
    )


    print(
        f"Auctions:        "
        f"{config['auctions']}"
    )


    print(
        f"RIT Period:      "
        f"{case_info.get('period')}"
    )


    print(
        f"Current Tick:    "
        f"{case_info.get('tick')}"
    )


    print(
        "=" * 70
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=" * 70
    )


    print(
        "LT3 / LT4 AUTOMATIC TRADING SYSTEM"
    )


    print(
        "Waiting for RIT case..."
    )


    print(
        "=" * 70
    )


    last_iteration = None


    while True:

        try:

            # ------------------------------------------------
            # READ CURRENT CASE
            # ------------------------------------------------

            case_info = (
                get_case()
            )


            # ------------------------------------------------
            # CASE STATUS
            # ------------------------------------------------

            status = (
                case_info.get(
                    "status"
                )
            )


            # ------------------------------------------------
            # WAIT UNTIL CASE IS ACTIVE
            # ------------------------------------------------

            if status != "ACTIVE":

                print(

                    f"Waiting for active case... "
                    f"Status: {status}",

                    end="\r"
                )


                time.sleep(
                    CHECK_INTERVAL
                )


                continue


            # ------------------------------------------------
            # DETECT ROUND FROM CASE NAME
            # ------------------------------------------------

            iteration = (
                detect_iteration(
                    case_info
                )
            )


            config = (
                ITERATION_CONFIG[
                    iteration
                ]
            )


            # ------------------------------------------------
            # PRINT WHEN ITERATION CHANGES
            # ------------------------------------------------

            if (
                iteration
                !=
                last_iteration
            ):

                print_iteration_info(

                    iteration,

                    config,

                    case_info
                )


                last_iteration = (
                    iteration
                )


            # ------------------------------------------------
            # START CURRENT STRATEGY
            # ------------------------------------------------
            #
            # The strategy runs until it detects that
            # the Round name has changed.
            #
            # Then it returns here.
            # ------------------------------------------------

            run_strategy(

                iteration,

                config
            )


            # ------------------------------------------------
            # SHORT PAUSE BEFORE READING NEXT ROUND
            # ------------------------------------------------

            time.sleep(
                CHECK_INTERVAL
            )


        # ====================================================
        # CTRL + C
        # ====================================================

        except KeyboardInterrupt:

            print(
                "\nSystem stopped by user."
            )

            break


        # ====================================================
        # ERROR HANDLING
        # ====================================================

        except Exception as error:

            print(
                f"\nMain error: {error}"
            )


            time.sleep(
                CHECK_INTERVAL
            )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()