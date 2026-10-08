# ============================================================
# ITERATION 7 (v2)
# CUSTOM SECURITIES - MAIN MARKET ONLY
# ============================================================
#
# CRZY:  Start $10 | Volatility 12% | High liquidity
# TAME:  Start $25 | Volatility 3%  | Low liquidity
# BBSN:  Start $45 | Volatility 7%  | Medium liquidity
#
# Main market only, API trading enabled.
# Tender evaluation / decision: automatic. Unwind: manual only.
# No cross-venue arbitrage, no auctions.
#
# v2 CHANGES
#
#   1. RISK HURDLE (all securities, not only BBSN)
#
#      A tender must pay, per share, at least
#
#          RISK_Z x sigma_tick x sqrt(T / 3)
#
#      after fees, impact and the model's risk terms.
#      sigma_tick is the dollar move per tick, T the ticks
#      needed to unwind (by liquidity), and sqrt(T / 3) the
#      risk of an inventory that shrinks linearly to zero.
#
#      Expensive or volatile names (BBSN at $45-85, TAME in
#      round 8) move several times more dollars per tick, so
#      the same $0.20 edge is eaten by drift during the
#      unwind. The hurdle scales with that automatically.
#
#      sigma_tick = max(observed, prior). The prior comes from
#      the case parameters: price x volatility / sqrt(300),
#      reading the case volatility as per round. It protects
#      the first ticks, when the observed history is empty.
#
#   2. UNWIND: LIMIT FIRST, MARKET WHEN EXPOSURE LASTS
#
#      Uses the iteration_5 execution layer: each order is
#      resolved after the execution delay (v1 cancelled
#      after 0.05 s, before a delayed order could execute),
#      positions are synced before the next order, and a
#      direction-reversal detector stops ping-pong.
#
#   3. Volatility history reset at the start of the round,
#      drawdown kill switch.
#
# Portions of this code were developed with assistance from
# OpenAI ChatGPT (2026) and Anthropic Claude (2026).
#
# ============================================================

import math
import time

from api import (
    get_case,
    get_round_number,
    get_tenders,
    get_securities,
    accept_tender,
    decline_tender,
)

import strategies.iteration_1 as tender_engine
import strategies.iteration_5 as ex


# ============================================================
# SETTINGS
# ============================================================

DRY_RUN = False

POLL_INTERVAL = 0.05

BOOK_LIMIT = 100

MAX_ORDER_SIZE = 10_000

TICKS_PER_PERIOD = 300


# ============================================================
# ROUND 7 PARAMETERS
# ============================================================

ITERATION_PARAMETERS = {

    "CRZY": {
        "start_price": 10.00,
        "volatility": 0.12,
        "liquidity": "HIGH"
    },

    "TAME": {
        "start_price": 25.00,
        "volatility": 0.03,
        "liquidity": "LOW"
    },

    "BBSN": {
        "start_price": 45.00,
        "volatility": 0.07,
        "liquidity": "MEDIUM"
    }
}


# ============================================================
# FEES / MODEL
# ============================================================

MAIN_FEE = 0.02

LAMBDA_INVENTORY = 0.002

LAMBDA_LIQUIDATION = 0.30

MIN_EXPECTED_PROFIT = 0.0

NET_POSITION_LIMIT = 100_000

GROSS_POSITION_LIMIT = 200_000


# ============================================================
# RISK HURDLE
# ============================================================

# Required edge per share = RISK_Z standard deviations of the
# dollar drift expected while unwinding. 0 disables it.
RISK_Z = 0.425

# Ticks expected to unwind a tender, by case liquidity.
UNWIND_TICKS_BY_LIQUIDITY = {
    "HIGH": 8,
    "MEDIUM": 12,
    "LOW": 18,
}

# sigma_tick = max(observed, prior). False = observed only.
USE_VOLATILITY_PRIOR = True


# ============================================================
# CONFIGURE / PREPARE ROUND
# ============================================================

def configure_round_7_engine():

    tender_engine.MAIN_FEE = MAIN_FEE
    tender_engine.LAMBDA_INVENTORY = LAMBDA_INVENTORY
    tender_engine.LAMBDA_LIQUIDATION = LAMBDA_LIQUIDATION
    tender_engine.MIN_EXPECTED_PROFIT = MIN_EXPECTED_PROFIT
    tender_engine.NET_POSITION_LIMIT = NET_POSITION_LIMIT
    tender_engine.GROSS_POSITION_LIMIT = GROSS_POSITION_LIMIT


def prepare_round(parameters=None):
    """
    Fresh state for a round (also used by iteration_8 with
    its own parameters).
    """

    global ITERATION_PARAMETERS

    if parameters is not None:
        ITERATION_PARAMETERS = parameters


    # Execution layer: order tracking, sync, kill switch.
    ex.reset_runtime_state()
    ex.DRY_RUN = DRY_RUN
    ex.load_security_meta()

    # Volatility history must not leak from the previous
    # round (BBSN $45 in round 7 vs $85 in round 8).
    tender_engine.price_history.clear()
    tender_engine.last_history_tick = None


# ============================================================
# BASIC HELPERS
# ============================================================

def remaining_quantity(order):

    quantity = float(order.get("quantity", 0) or 0)
    filled = float(order.get("quantity_filled", 0) or 0)

    return max(quantity - filled, 0)


def get_base_ticker(ticker):

    return str(ticker).split("_")[0]


def get_best_order(book, side):
    """
    Best price level with all quantity resting at it.
    """

    return ex.get_best_order(book, side)


def get_base_position(base, securities):

    total = 0

    for security in securities:

        if get_base_ticker(security.get("ticker", "")) == base:
            total += int(security.get("position", 0) or 0)

    return total


def get_main_markets(securities):

    markets = {}

    for security in securities:

        ticker = security.get("ticker", "")

        if not ticker:
            continue

        base = get_base_ticker(ticker)

        if base in markets:
            continue

        try:
            markets[base] = tender_engine.resolve_main_ticker(ticker, securities)
        except Exception:
            continue

    return markets


# ============================================================
# RISK HURDLE
# ============================================================

def sigma_tick_prior(base, price):
    """
    Dollar volatility per tick implied by the case parameters,
    reading the case volatility as per round:

        price x volatility / sqrt(ticks per round)
    """

    parameters = ITERATION_PARAMETERS.get(base)

    if not parameters or price is None or price <= 0:
        return 0.0

    return price * parameters["volatility"] / math.sqrt(TICKS_PER_PERIOD)


def sigma_tick_used(base, price, observed):

    if not USE_VOLATILITY_PRIOR:
        return observed

    return max(observed or 0.0, sigma_tick_prior(base, price))


def risk_hurdle(base, sigma_tick):
    """
    Returns (hurdle_per_share, unwind_ticks_estimate).
    """

    liquidity = ITERATION_PARAMETERS.get(base, {}).get("liquidity", "MEDIUM")
    ticks = UNWIND_TICKS_BY_LIQUIDITY.get(liquidity, 12)

    hurdle = RISK_Z * sigma_tick * math.sqrt(ticks / 3.0)

    return hurdle, ticks


def print_risk_table():

    print("RISK HURDLE BY SECURITY (start price, prior sigma):")

    for base, parameters in ITERATION_PARAMETERS.items():

        sigma = sigma_tick_prior(base, parameters["start_price"])
        hurdle, ticks = risk_hurdle(base, sigma)

        print(
            f"  {base:<5} ${parameters['start_price']:>6.2f} | "
            f"sigma/tick ${sigma:.3f} | unwind ~{ticks} ticks | "
            f"edge needed ${hurdle:.3f}/share beyond costs"
        )


# ============================================================
# TENDER EVALUATION
# ============================================================

def evaluate_tender_with_risk(tender, securities):
    """
    iteration_1 model, then:
      - liquidation risk recomputed with sigma_used
      - per-share hurdle from the dollar drift risk
    """

    result = tender_engine.evaluate_tender(tender, securities)

    if result.get("expected_profit") is None:
        return result

    base = get_base_ticker(tender["ticker"])
    quantity = float(result["quantity"])
    price = float(result["tender_price"])

    observed = float(result.get("volatility") or 0.0)
    prior = sigma_tick_prior(base, price)
    sigma = sigma_tick_used(base, price, observed)

    liquidation_used = tender_engine.calculate_liquidation_risk(
        quantity, result["visible_depth"], sigma
    )

    expected_profit = (
        result["expected_profit"]
        - (liquidation_used - result["liquidation_risk"])
    )

    hurdle, ticks = risk_hurdle(base, sigma)
    per_share = expected_profit / quantity if quantity > 0 else 0.0

    result.update({
        "volatility_observed": observed,
        "volatility_prior": prior,
        "volatility": sigma,
        "liquidation_risk": liquidation_used,
        "expected_profit": expected_profit,
        "expected_profit_per_share": per_share,
        "risk_hurdle": hurdle,
        "unwind_ticks_estimate": ticks,
    })

    if expected_profit <= MIN_EXPECTED_PROFIT:
        result["decision"] = "DECLINE"
        result["reason"] = "Expected profit not positive"

    elif per_share < hurdle:
        result["decision"] = "DECLINE"
        result["reason"] = (
            f"Risk hurdle: ${per_share:.4f}/share < ${hurdle:.4f}/share "
            f"({RISK_Z} sd of drift over ~{ticks} ticks)"
        )

    else:
        result["decision"] = "ACCEPT"
        result["reason"] = (
            f"${per_share:.4f}/share >= risk hurdle ${hurdle:.4f}/share"
        )

    return result


def print_risk_block(result):

    if "risk_hurdle" not in result:
        return

    print(
        f"RISK | sigma/tick observed ${result['volatility_observed']:.4f} "
        f"| prior ${result['volatility_prior']:.4f} "
        f"| used ${result['volatility']:.4f}"
    )
    print(
        f"RISK | expected ${result['expected_profit_per_share']:.4f}/share "
        f"| hurdle ${result['risk_hurdle']:.4f}/share "
        f"| -> {result['decision']} ({result['reason']})"
    )
    print("=" * 65)


# ============================================================
# PROCESS TENDERS
# ============================================================

def process_tenders(processed_tenders, securities):

    for tender in get_tenders():

        tender_id = tender["tender_id"]

        if tender_id in processed_tenders:
            continue

        if tender.get("is_fixed_bid", True) is False:
            print(f"\nUnexpected auction tender {tender_id} in Round 7.")
            continue

        try:

            result = evaluate_tender_with_risk(tender, securities)

            tender_engine.print_tender_result(result)
            print_risk_block(result)

            if result["decision"] == "ACCEPT":

                if not DRY_RUN:
                    accept_tender(tender_id)

                print(
                    "\nDRY RUN: Tender would be ACCEPTED." if DRY_RUN
                    else "\nTender accepted. MANUAL unwind only."
                )

            else:

                if not DRY_RUN:
                    decline_tender(tender_id)

                print(
                    "\nDRY RUN: Tender would be DECLINED." if DRY_RUN
                    else "\nTender declined."
                )

            processed_tenders.add(tender_id)

        except Exception as error:

            print(f"\nTender {tender_id} error: {error}")


# ============================================================
# DISPLAY ROUND 7 PARAMETERS
# ============================================================

def print_round_7_parameters():

    print()
    print("-" * 70)
    print("CRZY | Start $10 | Vol 12% | High Liquidity")
    print("TAME | Start $25 | Vol 3%  | Low Liquidity")
    print("BBSN | Start $45 | Vol 7%  | Medium Liquidity")
    print("-" * 70)
    print_risk_table()
    print("-" * 70)
    print()


# ============================================================
# ITERATION 7 MAIN LOOP
# ============================================================

def run_iteration_7(config):

    global TICKS_PER_PERIOD

    configure_round_7_engine()
    prepare_round()

    print("=" * 70)
    print("ITERATION 7 - MANUAL UNWIND")
    print("CUSTOM SECURITIES | MAIN MARKET ONLY")
    print("=" * 70)
    print("Mode:                " + ("DRY RUN" if DRY_RUN else "LIVE"))
    print("Tender Decision:     AUTOMATIC + RISK HURDLE")
    print(f"Risk Hurdle:         {RISK_Z} sd of $ drift during unwind")
    print("Position Unwind:     MANUAL ONLY - no orders submitted")
    print(f"Main Fee:            ${MAIN_FEE:.3f}/share")
    print(f"Kill Switch:         {ex.MAX_DRAWDOWN}")
    print("=" * 70)

    print_round_7_parameters()
    ex.print_security_meta()

    processed_tenders = set()

    while True:

        try:

            case_info = get_case()

            current_iteration = get_round_number(case_info)

            if current_iteration != config["iteration"]:
                print(
                    f"\nIteration {config['iteration']} ended. "
                    f"Switching to Iteration {current_iteration}."
                )
                return

            if case_info.get("status") != "ACTIVE":
                print("\nCase is not active. Returning to main router.")
                return

            current_tick = int(case_info.get("tick", 0))
            TICKS_PER_PERIOD = int(case_info.get("ticks_per_period") or TICKS_PER_PERIOD)

            securities = get_securities()
            tender_engine.update_market_history(securities, current_tick)

            if ex.check_kill_switch():
                time.sleep(0.25)
                continue

            # 1. TENDERS
            process_tenders(processed_tenders, securities)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:

            print("\nIteration 7 stopped by user.")
            return

        except Exception as error:

            print(f"\nIteration 7 error: {error}")
            time.sleep(POLL_INTERVAL)