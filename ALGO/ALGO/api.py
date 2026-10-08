# ============================================================
# RIT REST API
# ============================================================
#
# Shared API functions used by main.py and all iterations.
#
# v2 (execution safety):
#   - HTTP 429 raises RateLimitException with the wait time
#     and order submission retries once after waiting
#   - respects the X-Wait-Until header per ticker before
#     sending the next order on that ticker
#   - new: get_order(order_id), cancel_all_orders(),
#          get_trader(), get_limits(), book_side()
#
# Portions of this code were developed with assistance from
# OpenAI ChatGPT (2026) and Anthropic Claude (2026).
#
# ============================================================

import re
import time

import requests


# ============================================================
# CONNECTION SETTINGS
# ============================================================

BASE_URL = "http://localhost:9999/v1"


# ============================================================
# API KEY
# ============================================================
#
# Must match the API key shown inside your RIT Client.
#
# ============================================================

API_KEY = {
    "X-API-Key": "Z81JOMHP"
}


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update(API_KEY)


# ============================================================
# EXCEPTIONS
# ============================================================

class ApiException(Exception):
    pass


class RateLimitException(ApiException):
    """
    Raised on HTTP 429. `wait` is the number of seconds RIT
    asks us to wait before trying again.
    """

    def __init__(self, message, wait):
        super().__init__(message)
        self.wait = wait


# ============================================================
# RESPONSE HELPERS
# ============================================================

def check_response(response):
    """
    Raises a useful error if the RIT API rejects a request.
    """

    if response.ok:
        return

    if response.status_code == 401:
        raise ApiException(
            "RIT API returned 401 Unauthorized. "
            "Check that the API key in api.py matches "
            "the API key in the RIT Client."
        )

    try:
        error_data = response.json()
    except Exception:
        error_data = {}

    if not isinstance(error_data, dict):
        error_data = {}

    message = (
        error_data.get("message")
        or error_data.get("code")
        or response.text
    )

    if response.status_code == 429:

        wait = (
            error_data.get("wait")
            or response.headers.get("Retry-After")
            or 0.25
        )

        try:
            wait = float(wait)
        except (TypeError, ValueError):
            wait = 0.25

        raise RateLimitException(
            f"RIT API rate limit: wait {wait:.3f}s",
            wait
        )

    raise ApiException(
        f"RIT API error {response.status_code}: {message}"
    )


def _parse(response):
    """
    Some successful calls return no JSON body.
    """

    if not response.text.strip():
        return None

    try:
        return response.json()
    except Exception:
        return response.text


# ============================================================
# ORDER THROTTLE (X-Wait-Until)
# ============================================================
#
# Every successful POST /orders returns X-Wait-Until: the
# number of seconds to wait before the next order on the
# same ticker. We store it and wait before the next order.
#
# ============================================================

_next_order_time = {}

MAX_HEADER_WAIT = 5.0


def _wait_for_order_slot(ticker):

    ready_at = _next_order_time.get(ticker)

    if ready_at is None:
        return

    delay = ready_at - time.time()

    if delay > 0:
        time.sleep(delay)


def _record_order_slot(ticker, response):

    header = response.headers.get("X-Wait-Until")

    if header is None:
        return

    try:
        wait = float(header)
    except ValueError:
        return

    # Ignore anything that does not look like a relative
    # number of seconds, so a surprise format can never
    # freeze the algorithm.
    if 0 <= wait <= MAX_HEADER_WAIT:
        _next_order_time[ticker] = time.time() + wait


def _post_order(params):
    """
    Sends one order. On 429, waits once and retries once.
    """

    ticker = params["ticker"]

    for attempt in range(2):

        _wait_for_order_slot(ticker)

        response = session.post(
            f"{BASE_URL}/orders",
            params=params
        )

        try:
            check_response(response)

        except RateLimitException as error:

            if attempt == 0 and error.wait <= 1.0:
                time.sleep(error.wait + 0.05)
                continue

            raise

        _record_order_slot(ticker, response)

        return _parse(response)


# ============================================================
# CASE
# ============================================================

def get_case():
    """
    Returns current case information, e.g.

    {"name": "Round 4", "period": 1, "tick": 139,
     "ticks_per_period": 300, "total_periods": 1,
     "status": "ACTIVE"}
    """

    response = session.get(f"{BASE_URL}/case")
    check_response(response)
    return response.json()


def get_round_number(case_info=None):
    """
    Extracts the evaluation round from the case name.

    We intentionally DO NOT use case_info["period"] because
    each Round is a separate one-period case on this server.
    """

    if case_info is None:
        case_info = get_case()

    case_name = str(case_info.get("name", ""))

    match = re.search(r"round\s*(\d+)", case_name, re.IGNORECASE)

    if match is None:
        raise ApiException(
            f"Could not identify evaluation round "
            f"from case name: {case_name}"
        )

    round_number = int(match.group(1))

    if not 1 <= round_number <= 8:
        raise ApiException(
            f"Invalid evaluation round: {round_number}"
        )

    return round_number


def get_tick():
    return get_case().get("tick")


def get_case_status():
    return get_case().get("status")


# ============================================================
# TRADER / LIMITS
# ============================================================

def get_trader():
    """
    Returns {"trader_id", "first_name", "last_name", "nlv"}.
    """

    response = session.get(f"{BASE_URL}/trader")
    check_response(response)
    return response.json()


def get_limits():
    """
    Returns RIT's own view of gross / net usage and limits:

    [{"name", "gross", "net", "gross_limit", "net_limit",
      "gross_fine", "net_fine"}]
    """

    response = session.get(f"{BASE_URL}/limits")
    check_response(response)
    return response.json()


# ============================================================
# SECURITIES
# ============================================================

def get_securities(ticker=None):
    """
    Without ticker: all securities.
    With ticker: information for that ticker.

    Useful per-ticker fields: position, trading_fee,
    limit_order_rebate, max_trade_size, execution_delay_ms,
    api_orders_per_second.
    """

    params = {}

    if ticker is not None:
        params["ticker"] = ticker

    response = session.get(f"{BASE_URL}/securities", params=params)
    check_response(response)
    return response.json()


def get_security(ticker):

    data = get_securities(ticker=ticker)

    if isinstance(data, list):

        if not data:
            raise ApiException(f"Security not found: {ticker}")

        return data[0]

    return data


def get_position(ticker):
    return int(get_security(ticker).get("position", 0))


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book(ticker, limit=100):
    """
    Returns {"bids": [...], "asks": [...]} for one security.
    """

    response = session.get(
        f"{BASE_URL}/securities/book",
        params={"ticker": ticker, "limit": limit}
    )

    check_response(response)
    return response.json()


def book_side(book, side):
    """
    Returns the list of orders for "bids" or "asks".

    The RIT spec documents the keys as "bid"/"ask" while the
    client returns "bids"/"asks". Accept both, so a version
    difference never turns into a silently empty book.
    """

    if not isinstance(book, dict):
        return []

    orders = book.get(side)

    if orders is None:
        orders = book.get(side.rstrip("s"))

    return orders or []


def get_best_bid(ticker):

    bids = book_side(get_order_book(ticker, limit=10), "bids")

    if not bids:
        return None

    return max(bids, key=lambda order: float(order["price"]))


def get_best_ask(ticker):

    asks = book_side(get_order_book(ticker, limit=10), "asks")

    if not asks:
        return None

    return min(asks, key=lambda order: float(order["price"]))


# ============================================================
# TENDERS
# ============================================================

def get_tenders():

    response = session.get(f"{BASE_URL}/tenders")
    check_response(response)
    return response.json()


def accept_tender(tender_id, price=None):
    """
    Accepts a fixed tender, or submits a price for an
    auction tender (price required when not fixed-bid).
    """

    params = {}

    if price is not None:
        params["price"] = price

    response = session.post(
        f"{BASE_URL}/tenders/{tender_id}",
        params=params
    )

    check_response(response)
    return _parse(response)


def decline_tender(tender_id):

    response = session.delete(f"{BASE_URL}/tenders/{tender_id}")
    check_response(response)
    return _parse(response)


# ============================================================
# ORDERS
# ============================================================

def submit_limit_order(ticker, action, quantity, price):
    """
    Sends a LIMIT order. Returns the Order object, including
    order_id, status, quantity_filled and vwap.
    """

    return _post_order({
        "ticker": ticker,
        "type": "LIMIT",
        "quantity": int(quantity),
        "action": action.upper(),
        "price": float(price),
    })


def submit_market_order(ticker, action, quantity):
    """
    Sends a MARKET order.

    Returns the Order object (order_id, status,
    quantity_filled, vwap). With an execution delay the
    response may still show status OPEN: poll get_order()
    until it resolves before trusting positions.
    """

    return _post_order({
        "ticker": ticker,
        "type": "MARKET",
        "quantity": int(quantity),
        "action": action.upper(),
    })


def get_orders(status=None):
    """
    Returns orders. NOTE: RIT defaults to status=OPEN, so
    filled or cancelled orders do not appear unless asked.
    """

    params = {}

    if status is not None:
        params["status"] = status

    response = session.get(f"{BASE_URL}/orders", params=params)
    check_response(response)
    return response.json()


def get_order(order_id):
    """
    Returns one order by id, whatever its status
    (OPEN, TRANSACTED or CANCELLED).
    """

    response = session.get(f"{BASE_URL}/orders/{order_id}")
    check_response(response)
    return response.json()


def cancel_order(order_id):

    response = session.delete(f"{BASE_URL}/orders/{order_id}")
    check_response(response)
    return _parse(response)


def cancel_all_orders(ticker=None):
    """
    Bulk cancel. With ticker: that security only.
    Without ticker: every open order.
    """

    params = {"ticker": ticker} if ticker else {"all": 1}

    response = session.post(
        f"{BASE_URL}/commands/cancel",
        params=params
    )

    check_response(response)
    return _parse(response)


# ============================================================
# TIME & SALES
# ============================================================

def get_time_and_sales(ticker, limit=None):
    """
    Returns Time & Sales for a security.

    NOTE: per the RIT spec, `limit` is a number of TICKS
    counted back from the current tick, not a number of
    trades.
    """

    params = {"ticker": ticker}

    if limit is not None:
        params["limit"] = int(limit)

    response = session.get(
        f"{BASE_URL}/securities/tas",
        params=params
    )

    check_response(response)
    return response.json()


# ============================================================
# TEST CONNECTION
# ============================================================

def test_connection():

    print("=" * 60)
    print("RIT API CONNECTION TEST")
    print("=" * 60)

    try:

        case_info = get_case()

        print("Connection:      SUCCESS")
        print(f"Case Name:       {case_info.get('name')}")
        print(f"Round Detected:  {get_round_number(case_info)}")
        print(f"RIT Period:      {case_info.get('period')}")
        print(f"Tick:            {case_info.get('tick')}")
        print(f"Ticks / Period:  {case_info.get('ticks_per_period')}")
        print(f"Status:          {case_info.get('status')}")
        print("=" * 60)

        return True

    except Exception as error:

        print("Connection:      FAILED")
        print(f"Error:           {error}")
        print("=" * 60)

        return False


if __name__ == "__main__":

    test_connection()