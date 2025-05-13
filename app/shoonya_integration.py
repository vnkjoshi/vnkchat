# shoonya_integration.py
"""
shoonya_integration.py - A dedicated module to interact with the Shoonya API.
This file wraps the key API functions: login, script search, daily price series,
quotes retrieval, order placement, and real-time data via websocket.
"""
import gevent
import time, json
from datetime import datetime, timedelta
import pyotp
from api_helper import ShoonyaApiPy  # Ensure you have the Shoonya API library installed
import logging
from app.metrics import HTTP_REQUESTS, SHOONYA_API_ERRORS, API_CALL_LATENCY
from .exceptions import ShoonyaAPIException

logger = logging.getLogger(__name__)

# ----------------------------
# Route: initialize_shoonya
# ----------------------------
def initialize_shoonya(user, retries=6, delay=12):
    """
    Initialize and log in to the Shoonya API using the user's stored API credentials.
    Retries the login process if a transient error occurs.
    """
    cred = user.api_credential
    if not cred:
        logger.error("❌ Shoonya API credentials are not set for user %s", user.email)
        return None
    if not (cred.shoonya_user_id and cred.shoonya_password and cred.vendor_code 
            and cred.api_secret and cred.imei and cred.totp_secret):
        logger.error("❌ Shoonya API credentials are incomplete for user %s", user.email)
        return None

    api = ShoonyaApiPy()
    for attempt in range(retries):
        attempt_start = time.time()
        try:
            with API_CALL_LATENCY.labels(api_method='login').time():
                login_response = api.login(
                    userid=cred.shoonya_user_id,
                    password=cred.shoonya_password,
                    twoFA=pyotp.TOTP(cred.totp_secret).now(),
                    vendor_code=cred.vendor_code,
                    api_secret=cred.api_secret,
                    imei=cred.imei
                )
            elapsed = time.time() - attempt_start
            logger.info("Attempt %s for user %s took %.2f seconds", attempt + 1, user.email, elapsed)
            if login_response and login_response.get("stat") == "Ok":
                logger.info("✅ Shoonya API login successful for user %s", user.email)
                # Add a short wait to allow live data feeds to become available.
                time.sleep(5)  # warm-up delay; adjust as necessary
                return api
            else:
                logger.error("❌ Shoonya API login failed for user %s: %s", user.email, login_response)
        except Exception as e:
            elapsed = time.time() - attempt_start
            logger.error("❌ Exception during Shoonya login for user %s (attempt %s, %.2fs): %s", user.email, attempt + 1, elapsed, e)
        gevent.sleep(delay)
    logger.error("❌ Exceeded maximum retries for Shoonya login for user %s", user.email)
    return None
# ----------------------------
# Route: search_script
# ----------------------------
def search_script(api, search_text, exchange="NSE"):
    """
    Wraps the Shoonya API call to search for trading instruments (scripts) by name on a given exchange.
    Logic: Logs and returns the search results, helping users identify which instruments to trade.
    """
    try:
        logger.info("✅ Calling searchscrip with search_text='%s', exchange='%s'", search_text, exchange)
        with API_CALL_LATENCY.labels(api_method='search_script').time():
            result = api.searchscrip(exchange=exchange, searchtext=search_text)
        logger.info("✅ Search result for '%s': %s", search_text, result)
        return result
    except Exception as e:
        logger.error(
            "❌ Shoonya API error in search_script (search_text=%s, exchange=%s): %s",
            search_text, exchange, e, exc_info=True
        )
        raise ShoonyaAPIException(
            f"search_script failed for {search_text}@{exchange}: {e}"
        ) from e
# ----------------------------
# Route: Implementing get_previous_ohlc
# ----------------------------
def get_previous_ohlc(api, tradingsymbol, field, exchange="NSE"):
    """
    Fetch the previous trading day's OHLC value for the given trading symbol and user-selected field.
    If yesterday's data is missing or zero, falls back to the most recent prior nonzero record.
    Uses api_daily_price_series (wrapper) to ensure consistent parameter handling and logging.
    """
    try:
        # Map user-friendly field to the API JSON key.
        field_map = {"open":"into","high":"inth","low":"intl","close":"intc"}

        key = field.lower()
        if key not in field_map:
            logger.error("❌ Invalid field '%s'. Must be one of open, high, low, close.", field)
            return None
        shoonya_key = field_map[key]

        # Compute 10 days ago and end of yesterday (exclude today's partial data).
        now = datetime.now()
        from_date = int((now - timedelta(days=10)).timestamp())
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = int(today_midnight.timestamp()) - 1

        # Fetch with unified wrapper
        series = api_daily_price_series(api, tradingsymbol, from_date, end_date, exchange=exchange)
        
        if not series:
            logger.warning("❌ No historical data returned for %s", tradingsymbol)
            return None
        logger.info("✅ Unified series for %s: %s", tradingsymbol, series)

        # Parse JSON records
        records = []
        for item in series:
            try:
                records.append(json.loads(item))
            except Exception as e:
                logger.error("❌ Error parsing record for %s: %s", tradingsymbol, e)
        if not records:
            logger.warning("❌ No valid records for %s", tradingsymbol)
            return None
        
        # --- Replace the old sort+loop with this ---
        def parse_date(rec):
            """Try ssboe timestamp first, else fall back to dd-MMM-YYYY."""
            if "ssboe" in rec:
                return datetime.fromtimestamp(int(rec["ssboe"])).date()
            try:
                return datetime.strptime(rec.get("time",""), "%d-%b-%Y").date()
            except:
                return datetime.min.date()

        # Sort descending by timestamp or date
        try:
            records.sort(key=lambda r: parse_date(r), reverse=True)
        except Exception as e:
            logger.error("❌ Error sorting historical data for %s: %s", tradingsymbol, e)
            return None

        # Skip any record from today, then return first nonzero
        today = datetime.now().date()
        # Find first non‑zero prior to today
        for rec in records:
            rec_date = parse_date(rec)
            if rec_date >= today:
                continue
            val = float(rec.get(field_map[field], 0) or 0)
            if val > 0:
                logger.info("✅ %s for %s = %s on %s", field, tradingsymbol, val, rec.get("time"))
                return val

        logger.warning("❌ No valid %s for %s", field, tradingsymbol)
        return None

    except ShoonyaAPIException:
        # let underlying API exceptions bubble
        raise
    except Exception as e:
        logger.error(
            "❌ Shoonya API error in get_previous_ohlc (symbol=%s, field=%s, exchange=%s): %s",
            tradingsymbol, field, exchange, e, exc_info=True
        )
        raise ShoonyaAPIException(
            f"get_previous_ohlc failed for {tradingsymbol}@{exchange} field {field}: {e}"
        ) from e

def api_daily_price_series(api, tradingsymbol, from_date, to_date=None, exchange="NSE"):
    """
    Fetch daily price series (close, high, open, low) for a given symbol.
    from_date and to_date should be provided as Unix timestamps.
    """
    # Optional: convert from_date to a string for logging purposes
    from_date_str = datetime.fromtimestamp(from_date).strftime("%d-%b-%Y")
    logger.info("✅ Fetching daily price series for %s from %s to %s", tradingsymbol, from_date_str, to_date)
    try:
        with API_CALL_LATENCY.labels(api_method='api_daily_price_series').time():
            result = api.get_daily_price_series(
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                startdate=from_date,
                enddate=to_date
            )
        logger.info("✅ Daily price series for %s: %s", tradingsymbol, result)
        return result
    except Exception as e:
        logger.error(
            "❌ Shoonya API error in api_daily_price_series (token=%s, from=%s, to=%s): %s",
            tradingsymbol, from_date, to_date, e,
            exc_info=True
        )
        raise ShoonyaAPIException(f"api_daily_price_series failed: {e}") from e    

def get_quotes(api, token, exchange="NSE"):
    """
    Get quotes for a given token.
    """
    try:
        logger.info("✅ Fetching quotes for token %s on exchange %s", token, exchange)
        with API_CALL_LATENCY.labels(api_method='get_quotes').time():
            result = api.get_quotes(exchange=exchange, token=token)
        logger.info("✅ Quotes for token %s: %s", token, result)
        return result
    except Exception as e:
        # Increment our Prometheus counter for this method
        SHOONYA_API_ERRORS.labels(api_method='get_quotes').inc()
        logger.error(
            "❌ Shoonya API error in get_quotes (token=%s, exchange=%s): %s",
            token, exchange, e, exc_info=True
        )
        raise ShoonyaAPIException(f"get_quotes failed for {token}@{exchange}: {e}") from e

def place_order(api, **order_params):
    """
    Place an order using the Shoonya API.
    """
    try:
        with API_CALL_LATENCY.labels(api_method='place_order').time():
            result = api.place_order(**order_params)
        logger.info("✅ Order placed with params %s: %s", order_params, result)
        return result
    except Exception as e:
        SHOONYA_API_ERRORS.labels(api_method='place_order').inc()
        logger.error(
            "❌ Shoonya API error in place_order (params=%s): %s",
            order_params, e, exc_info=True
        )
        raise ShoonyaAPIException(f"place_order failed: {e}") from e