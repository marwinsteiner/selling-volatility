import pandas as pd
import numpy as np
import requests
import shelve

from datetime import datetime, timedelta
from pandas_market_calendars import get_calendar
from dynaconf import Dynaconf
from typing import Literal
from pathlib import Path

# Load settings and secrets
settings = Dynaconf(
    settings_files=['settings.json', '.secrets.json'],
)

EnvironmentType = Literal['sandbox', 'production']  # create a type alias

ENVIRONMENT: EnvironmentType = 'sandbox'


def get_session_token(environment: EnvironmentType):
    """
    The get_session_token function retrieves and stores a tastytrade session token depending on whether you want to
    log into a sandbox or a production environment.

    # Example usage
    if token := get_session_token(environment='sandbox'):
        print(f"Session token: {token}")
    """
    with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
        session_token = db.get('session_token')
        token_expiry = db.get('token_expiry')

        # Check if we have a valid token that hasn't expired
        if session_token and token_expiry and datetime.now() < token_expiry:
            print("Using existing valid session token.")
            print(f'Session Token: {session_token}')
            print(f'Token Expiry: {token_expiry}')
            return session_token

    # If we get here, we either don't have a token or it's expired
    if environment == 'sandbox':
        url = f"{settings.TASTY_SANDBOX_BASE_URL}/sessions"
        print(url)
        payload = {
            "login": settings.TASTY_SANDBOX.USERNAME,
            "password": settings.TASTY_SANDBOX.PASSWORD
        }
        print(payload)
    else:
        url = f"{settings.TASTY_PRODUCTION_BASE_URL}/sessions"
        print(url)
        payload = {
            "login": settings.TASTY_PRODUCTION.USERNAME,
            "password": settings.TASTY_PRODUCTION.PASSWORD
        }
        print(payload)

    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 201:
        data = response.json()
        new_session_token = data['data']['session-token']
        new_token_expiry = datetime.now() + timedelta(hours=24)

        # Store the new token and expiry
        db['session_token'] = new_session_token
        db['token_expiry'] = new_token_expiry

        print("New session token generated and saved.")
        return new_session_token
    else:
        print(f"Error: {response.status_code}")
        print(response.text)
        return None


def account_information_and_balances(session_token):
    accounts = requests.get(f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox' else
    settings.TASTY_PRODUCTION_BASE_URL}/customers/me/accounts", headers={'Authorization': session_token}).json()
    account_number = accounts["data"]["items"][0]["account"]["account-number"]

    balances = \
        requests.get(f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox' else
        settings.TASTY_PRODUCTION_BASE_URL}/accounts/{account_number}/balances",
                     headers={'Authorization': session_token}).json()[
            "data"]

    option_buying_power = np.float64(balances["derivative-buying-power"])
    print(f"Buying Power: ${option_buying_power}")

    return account_number, option_buying_power


def get_trading_dates():
    # Logic to get trading dates
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
    # Logic to fetch and process VIX data to get the vol regime
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
    # Logic to fetch and process underlying data
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
    return big_underlying_data['regime'].iloc[-1]  # underlying regime


def calculate_expected_move():
    # Calculate expected move
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

        short_ticker_polygon = short_put["ticker"].iloc[0]
        long_ticker_polygon = long_put["ticker"].iloc[0]

        return short_strike, long_strike, short_ticker_polygon, long_ticker_polygon


def get_option_chain_data(session_token, short_strike, long_strike, trend_regime):
    """
    This function pulls the option chain from tasty. Sample usage:

    # Call the function with the necessary parameters including trend_regime
    trend_regime = get_underlying_regime(polygon_api_key=settings.POLYGON.API_KEY)
    short_strike, long_strike, short_ticker_polygon, long_ticker_polygon = calculate_expected_move()
    short_ticker, long_ticker = get_option_chain_data(session_token, short_strike, long_strike, trend_regime)
    """
    global short_ticker, long_ticker
    option_url = (f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox'
    else settings.TASTY_PRODUCTION_BASE_URL}/option-chains/SPXW/nested")

    option_chain = pd.json_normalize(
        requests.get(option_url, headers={'Authorization': session_token}).json()["data"]["items"][0]["expirations"][0][
            "strikes"])
    option_chain["strike_price"] = option_chain["strike-price"].astype(float)

    short_option = option_chain[option_chain["strike_price"] == short_strike].copy()
    long_option = option_chain[option_chain["strike_price"] == long_strike].copy()

    if trend_regime == 0:
        short_ticker = short_option["call"].iloc[0]
        long_ticker = long_option["call"].iloc[0]
        return short_ticker, long_ticker
    elif trend_regime == 1:
        short_ticker = short_option["put"].iloc[0]
        long_ticker = long_option["put"].iloc[0]
        return short_ticker, long_ticker


def get_option_quotes(polygon_api_key=settings.POLYGON.API_KEY):
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
    session_token = get_session_token(environment=ENVIRONMENT)
    if not session_token:
        print('Failed to get valid session token.')
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
    print(validate_order.text)

    submit_order = requests.post(f"{settings.TASTY_SANDBOX_BASE_URL if ENVIRONMENT == 'sandbox' else
    settings.TASTY_PRODUCTION_BASE_URL}/accounts/{account_number}/orders", json=order_details,
                                 headers={'Authorization': session_token})
    return submit_order.text


if __name__ == '__main__':
    submit_order()
