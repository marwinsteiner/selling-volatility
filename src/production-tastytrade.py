import pandas as pd
import numpy as np
import requests
import shelve

from datetime import datetime, timedelta
from pandas_market_calendars import get_calendar
from dynaconf import Dynaconf
from typing import Literal
from pathlib import Path
from loguru import logger

# Set up a logging directory
log_dir = Path(__file__).parent / 'logs'
log_dir.mkdir(exist_ok=True)

# Create log file path with timestamp
log_file = log_dir / f"tastytrade_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# Configure logger to write to both console and file
logger.add(log_file, rotation="1 day")

# Load settings and secrets
settings = Dynaconf(
    settings_files=['settings.json', '.secrets.json'],
)

EnvironmentType = Literal['sandbox', 'production']  # create a type alias

# ENVIRONMENT toggles between sandbox (testing) and production (live trading)
ENVIRONMENT: EnvironmentType = 'production'
logger.info(f'Using environment: {ENVIRONMENT}')


def get_session_token(environment: EnvironmentType):
    """
    Get or generate a session token based on the environment.

    Args:
        environment (str): The environment type ('sandbox' or 'production').

    Returns:
        str: The session token if found or generated, None if the request fails.

    Examples:
        session_token = get_session_token('sandbox')
    """
    with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
        session_token = db.get('session_token')
        token_expiry = db.get('token_expiry')

        # Check if we have a valid token that hasn't expired
        if session_token and token_expiry and datetime.now() < token_expiry:
            logger.success('Found existing session token.', extra={'session_token': session_token})
            logger.info(f'Existing session token will expire at {token_expiry}.')
            return session_token

    # If we get here, we either don't have a token or it's expired
    logger.warning('Session token expired or invalid, generating new session token...')
    if environment == 'sandbox':
        url = f"{settings.TASTY_SANDBOX_BASE_URL}/sessions"
        logger.info(f'Using environment:{environment} with base url: {url}')
        payload = {
            "login": settings.TASTY_SANDBOX.USERNAME,
            "password": settings.TASTY_SANDBOX.PASSWORD
        }
    else:
        url = f"{settings.TASTY_PRODUCTION_BASE_URL}/sessions"
        logger.info(f'Using environment:{environment} with base url: {url}')
        payload = {
            "login": settings.TASTY_PRODUCTION.USERNAME,
            "password": settings.TASTY_PRODUCTION.PASSWORD
        }
    logger.debug('Generated payload.')
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers)
    logger.info(f'Posted request: {response}')

    if response.status_code == 201:
        logger.success(f'Response status code: {response.status_code}. Received session token.')
        data = response.json()
        new_session_token = data['data']['session-token']
        new_token_expiry = datetime.now() + timedelta(hours=24)
        logger.debug(f'Saved new session token expiring at: {new_token_expiry}.')

        # Open a new shelf connection to store the token
        with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
            db['session_token'] = new_session_token
            db['token_expiry'] = new_token_expiry
            logger.success('Stored new session token and token expiry.')

        return new_session_token
    else:
        logger.error(f'Session token request failed with response code: {response.status_code}.')
        logger.debug(f'{response.text}')
        return None


def account_information_and_balances(session_token):
    """
    Retrieve account information and balances using the provided session token.

    Args:
        session_token (str): The session token for authentication.

    Returns:
        tuple: A tuple containing the account number and option buying power.

    Raises:
        None
    """
    accounts = requests.get(f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox' else
    settings.TASTY_PRODUCTION_BASE_URL}/customers/me/accounts", headers={'Authorization': session_token}).json()
    account_number = accounts["data"]["items"][0]["account"]["account-number"]

    if accounts:
        logger.success(f'Retrieved account information for account {account_number} - a {ENVIRONMENT} account.')
    else:
        logger.warning(f'Unable to retrieve account information. Failed with error {accounts.text}.')

    balances = requests.get(f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox'
    else settings.TASTY_PRODUCTION_BASE_URL}/accounts/{account_number}/balances",
                            headers={'Authorization': session_token}).json()["data"]

    if balances:
        logger.success(f'Retrieved account balances for account {account_number} - a {ENVIRONMENT} account.')
    else:
        logger.warning(f'Unable to retrieve balances. Failed with error {balances.text}')

    option_buying_power = np.float64(balances["derivative-buying-power"])
    logger.info(f"Option Buying Power: ${option_buying_power}")

    return account_number, option_buying_power


def get_trading_dates():
    """
    Get the trading dates based on the NYSE calendar.

    Returns:
        numpy.ndarray: An array of trading dates in the format "%Y-%m-%d".

    Raises:
        None
    """
    calendar = get_calendar('NYSE')
    return (
        calendar.schedule(
            start_date="2023-01-01",
            end_date=datetime.now() + timedelta(days=1),
        )
        .index.strftime("%Y-%m-%d")
        .values
    )


def get_vol_regime(polygon_api_key=settings.POLYGON.API_KEY):
    """
    Get the volatility regime based on VIX data.

    Args:
        polygon_api_key (str): The API key for Polygon data (default is settings.POLYGON.API_KEY).

    Returns:
        int: The volatility regime based on the VIX data.

    Raises:
        None
    """
    date = get_trading_dates()[-1]

    vix_data = pd.json_normalize(requests.get(
        f"https://api.polygon.io/v2/aggs/ticker/I:VIX1D/range/1/day/2023-05-01/{date}?sort=asc&limit=50000&apiKey="
        f"{polygon_api_key}").json()["results"]).set_index("t")

    vix_data.index = pd.to_datetime(vix_data.index, unit="ms", utc=True).tz_convert("America/New_York")
    vix_data["1_mo_avg"] = vix_data["c"].rolling(window=30).mean()
    vix_data["3_mo_avg"] = vix_data["c"].rolling(window=63).mean()
    vix_data["6_mo_avg"] = vix_data["c"].rolling(window=126).mean()
    vix_data['vol_regime'] = vix_data.apply(lambda row: 1 if (row['1_mo_avg'] > row['3_mo_avg']) else 0, axis=1)
    vix_data["str_date"] = vix_data.index.strftime("%Y-%m-%d")

    return vix_data["vol_regime"].iloc[-1]  # vol regime


def get_underlying_regime(polygon_api_key=settings.POLYGON.API_KEY):
    """
    Get the regime of the underlying asset based on historical data.

    Args:
        polygon_api_key (str): The API key for Polygon data (default is settings.POLYGON.API_KEY).

    Returns:
        int: The regime of the underlying asset based on historical data.

    Raises:
        None
    """
    date = get_trading_dates()[-1]

    big_underlying_data = pd.json_normalize(requests.get(
        f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/2020-01-01/{date}"
        f"?adjusted=true&sort=asc&limit=50000&apiKey={polygon_api_key}").json()["results"]).set_index("t")
    big_underlying_data.index = pd.to_datetime(big_underlying_data.index, unit="ms", utc=True).tz_convert(
        "America/New_York")
    big_underlying_data["1_mo_avg"] = big_underlying_data["c"].rolling(window=20).mean()
    big_underlying_data["3_mo_avg"] = big_underlying_data["c"].rolling(window=60).mean()
    big_underlying_data['regime'] = big_underlying_data.apply(lambda row: 1 if (row['c'] > row['1_mo_avg']) else 0,
                                                              axis=1)
    logger.info('Retrieved and calculated underlying regime.')
    return big_underlying_data['regime'].iloc[-1]  # underlying regime


def calculate_expected_move():
    """
    Calculate the expected move and generate options trading strategies based on the trend regime.

    Returns:
        tuple: For trend regime 0, returns short_strike, long_strike, short_ticker_polygon, long_ticker_polygon.
               For trend regime 1, returns short_strike, long_strike, short_ticker_polygon, long_ticker_polygon.

    Raises:
        None
    """

    ticker = "I:SPX"
    index_ticker = "I:VIX1D"
    options_ticker = "SPX"

    trend_regime = get_underlying_regime(polygon_api_key=settings.POLYGON.API_KEY)

    trading_date = datetime.now().strftime("%Y-%m-%d")

    underlying_data = pd.json_normalize(requests.get(
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{trading_date}/{trading_date}"
        f"?adjusted=true&sort=asc&limit=50000&apiKey={settings.POLYGON.API_KEY}").json()[
                                            "results"]).set_index("t")
    underlying_data.index = pd.to_datetime(underlying_data.index, unit="ms", utc=True).tz_convert("America/New_York")

    index_data = pd.json_normalize(requests.get(
        f"https://api.polygon.io/v2/aggs/ticker/{index_ticker}/range/1/minute/{trading_date}/{trading_date}"
        f"?adjusted=true&sort=asc&limit=50000&apiKey={settings.POLYGON.API_KEY}").json()[
                                       "results"]).set_index("t")
    index_data.index = pd.to_datetime(index_data.index, unit="ms", utc=True).tz_convert("America/New_York")

    market_open = pd.Timestamp("09:35").time()

    # Remove .dt since we're working with a DatetimeIndex
    index_price = index_data[index_data.index.time >= market_open]["c"].iloc[0]
    price = underlying_data[underlying_data.index.time >= market_open]["c"].iloc[0]

    expected_move = (round((index_price / np.sqrt(252)), 2) / 100) * .50

    logger.success('Calculated expected move.')

    exp_date = trading_date  # strictly speaking, this is unnecessary -- just for humans to understand.

    if trend_regime == 0:
        valid_calls = pd.json_normalize(requests.get(
            f"https://api.polygon.io/v3/reference/options/contracts?underlying_ticker={options_ticker}"
            f"&contract_type=call&as_of={trading_date}&expiration_date={exp_date}&limit=1000&apiKey="
            f"{settings.POLYGON.API_KEY}").json()["results"])
        valid_calls = valid_calls[valid_calls["ticker"].str.contains("SPXW")].copy()
        valid_calls["days_to_exp"] = (
                pd.to_datetime(valid_calls["expiration_date"]) - pd.to_datetime(trading_date)).dt.days
        valid_calls["distance_from_price"] = abs(valid_calls["strike_price"] - price)

        upper_price = round(price + (price * expected_move))
        otm_calls = valid_calls[valid_calls["strike_price"] >= upper_price]

        short_call = otm_calls.iloc[[0]]
        long_call = otm_calls.iloc[[1]]

        short_strike = short_call["strike_price"].iloc[0]
        long_strike = long_call["strike_price"].iloc[0]

        logger.info(f'Generated Call-Side Trade | Short: {short_strike} Long: {long_strike}')

        short_ticker_polygon = short_call["ticker"].iloc[0]
        long_ticker_polygon = long_call["ticker"].iloc[0]

        return short_strike, long_strike, short_ticker_polygon, long_ticker_polygon

    elif trend_regime == 1:

        valid_puts = pd.json_normalize(requests.get(
            f"https://api.polygon.io/v3/reference/options/contracts?underlying_ticker={options_ticker}"
            f"&contract_type=put&as_of={trading_date}&expiration_date={exp_date}&limit=1000&apiKey="
            f"{settings.POLYGON.API_KEY}").json()["results"])
        valid_puts = valid_puts[valid_puts["ticker"].str.contains("SPXW")].copy()
        valid_puts["days_to_exp"] = (
                pd.to_datetime(valid_puts["expiration_date"]) - pd.to_datetime(trading_date)).dt.days
        valid_puts["distance_from_price"] = abs(price - valid_puts["strike_price"])

        lower_price = round(price - (price * expected_move))
        otm_puts = valid_puts[valid_puts["strike_price"] <= lower_price].sort_values("distance_from_price",
                                                                                     ascending=True)

        short_put = otm_puts.iloc[[0]]
        long_put = otm_puts.iloc[[1]]

        short_strike = short_put["strike_price"].iloc[0]
        long_strike = long_put["strike_price"].iloc[0]

        logger.info(f'Generated Put-Side Trade | Short: {short_strike} Long: {long_strike}')

        short_ticker_polygon = short_put["ticker"].iloc[0]
        long_ticker_polygon = long_put["ticker"].iloc[0]

        return short_strike, long_strike, short_ticker_polygon, long_ticker_polygon


def get_option_chain_data(session_token, short_strike, long_strike, trend_regime):
    """
    Retrieve option chain data based on the session token, strike prices, and trend regime.

    Args:
        session_token (str): The session token for authentication.
        short_strike (float): The strike price for the short option.
        long_strike (float): The strike price for the long option.
        trend_regime (int): The trend regime value (0 for call-side, 1 for put-side).

    Returns:
        tuple: For trend regime 0, returns short_ticker, long_ticker for call-side.
               For trend regime 1, returns short_ticker, long_ticker for put-side.

    Raises:
        None
    """
    global short_ticker, long_ticker
    option_url = (f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox'
    else settings.TASTY_PRODUCTION_BASE_URL}/option-chains/SPXW/nested")

    option_chain = pd.json_normalize(
        requests.get(option_url, headers={'Authorization': session_token}).json()["data"]["items"][0]["expirations"][0][
            "strikes"])
    option_chain["strike_price"] = option_chain["strike-price"].astype(float)

    logger.success('Retrieved nested option chain.')

    short_option = option_chain[option_chain["strike_price"] == short_strike].copy()
    long_option = option_chain[option_chain["strike_price"] == long_strike].copy()

    if trend_regime == 0:
        short_ticker = short_option["call"].iloc[0]
        long_ticker = long_option["call"].iloc[0]

        if short_ticker and long_ticker:
            logger.success('Retrieved call-side tickers.')
        else:
            logger.error('Failed to retrieve call-side tickers.')

        return short_ticker, long_ticker

    elif trend_regime == 1:
        short_ticker = short_option["put"].iloc[0]
        long_ticker = long_option["put"].iloc[0]

        if short_ticker and long_ticker:
            logger.success('Retrieved put-side tickers.')
        else:
            logger.error('Failed to retrieve put-side tickers.')

        return short_ticker, long_ticker


def get_option_quotes(polygon_api_key=settings.POLYGON.API_KEY):
    """
    Retrieve option quotes based on the calculated expected move.

    Args:
        polygon_api_key (str): The API key for Polygon data (default is settings.POLYGON.API_KEY).

    Returns:
        tuple: The natural price, mid-price, and optimal price of the option quotes.

    Raises:
        None
    """

    short_strike, long_strike, short_ticker_polygon, long_ticker_polygon = calculate_expected_move()

    short_option_quote = pd.json_normalize(requests.get(
        f"https://api.polygon.io/v3/quotes/{short_ticker_polygon}?&sort=timestamp&order=desc&limit=10&apiKey="
        f"{polygon_api_key}").json()["results"]).set_index("sip_timestamp").sort_index().tail(1)
    short_option_quote.index = pd.to_datetime(short_option_quote.index, unit="ns", utc=True).tz_convert(
        "America/New_York")

    long_option_quote = pd.json_normalize(requests.get(
        f"https://api.polygon.io/v3/quotes/{long_ticker_polygon}?&sort=timestamp&order=desc&limit=10&apiKey="
        f"{polygon_api_key}").json()["results"]).set_index("sip_timestamp").sort_index().tail(1)
    long_option_quote.index = pd.to_datetime(long_option_quote.index, unit="ns", utc=True).tz_convert(
        "America/New_York")

    natural_price = round(short_option_quote["bid_price"].iloc[0] - long_option_quote["ask_price"].iloc[0], 2)

    mid_price = round(((short_option_quote["bid_price"].iloc[0] + short_option_quote["ask_price"].iloc[0]) / 2) - (
            (long_option_quote["bid_price"].iloc[0] + long_option_quote["ask_price"].iloc[0]) / 2), 2)

    optimal_price = round(np.int64(round((mid_price - .05) / .05, 2)) * .05, 2)

    return natural_price, mid_price, optimal_price


def submit_order():
    """
    Submit an options trading order based on the calculated parameters and session information.

    Returns:
        str: The response text indicating the status of the order submission.

    Raises:
        None
    """
    session_token = get_session_token(environment=ENVIRONMENT)
    if not session_token:
        logger.warning('Failed to get valid session token.')
        return
    account_number, option_buying_power = account_information_and_balances(session_token=session_token)
    vol_regime = get_vol_regime()  # Add this
    trend_regime = get_underlying_regime()
    short_strike, long_strike, short_ticker_polygon, long_ticker_polygon = calculate_expected_move()
    natural_price, mid_price, optimal_price = get_option_quotes()
    short_ticker, long_ticker = get_option_chain_data(session_token=session_token,
                                                      short_strike=short_strike,
                                                      long_strike=long_strike,
                                                      trend_regime=trend_regime)
    order_details = {
        "time-in-force": "Day",
        "order-type": "Limit",
        "price": optimal_price,
        "price-effect": "Credit",
        "legs": [{"action": "Buy to Open",
                  "instrument-type": "Equity Option",
                  "symbol": f"{long_ticker}",
                  "quantity": 1},

                 {"action": "Sell to Open",
                  "instrument-type": "Equity Option",
                  "symbol": f"{short_ticker}",
                  "quantity": 1}]

    }

    # Do an order dry-run to make sure the trade will go through (i.e., verifies balance, valid symbol, etc. )

    validate_order = requests.post(f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox' else
    settings.TASTY_PRODUCTION_BASE_URL}/accounts/{account_number}/orders/dry-run",
                                   json=order_details, headers={'Authorization': session_token})

    if validate_order:
        logger.success('Validated order, no issues in preflight.')
    else:
        logger.warning(f'Validate order failed with issue in preflight: {validate_order.text}')

    submit_order = requests.post(f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox' else
    settings.TASTY_PRODUCTION_BASE_URL}/accounts/{account_number}/orders", json=order_details,
                                 headers={'Authorization': session_token})

    if submit_order:
        logger.success('Sent order.')
    else:
        logger.error(f'Failed to send order with error {submit_order.text}')

    return submit_order.text


if __name__ == '__main__':
    submit_order()
