# -*- coding: utf-8 -*-
"""
Created in 2024

@author: Quant Galore
"""

import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import time

from datetime import datetime, timedelta
from pandas_market_calendars import get_calendar

# from self_email import send_message

polygon_api_key = "KkfCQ7fsZnx0yK4bhX9fD81QplTh0Pf3"
calendar = get_calendar("NYSE")

trading_dates = calendar.schedule(start_date="2023-01-01",
                                  end_date=(datetime.today() + timedelta(days=1))).index.strftime("%Y-%m-%d").values

date = trading_dates[-1]

vix_data = pd.json_normalize(requests.get(
    f"https://api.polygon.io/v2/aggs/ticker/I:VIX1D/range/1/day/2023-05-01/{date}?sort=asc&limit=50000&apiKey={polygon_api_key}").json()[
                                 "results"]).set_index("t")
vix_data.index = pd.to_datetime(vix_data.index, unit="ms", utc=True).tz_convert("America/New_York")
vix_data["1_mo_avg"] = vix_data["c"].rolling(window=20).mean()
vix_data["3_mo_avg"] = vix_data["c"].rolling(window=60).mean()
vix_data['vol_regime'] = vix_data.apply(lambda row: 1 if (row['1_mo_avg'] > row['3_mo_avg']) else 0, axis=1)
vix_data["str_date"] = vix_data.index.strftime("%Y-%m-%d")
# vix_data = vix_data.set_index("str_date")

vol_regime = vix_data["vol_regime"].iloc[-1]

big_underlying_data = pd.json_normalize(requests.get(
    f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/2020-01-01/{date}?adjusted=true&sort=asc&limit=50000&apiKey={polygon_api_key}").json()[
                                            "results"]).set_index("t")
big_underlying_data.index = pd.to_datetime(big_underlying_data.index, unit="ms", utc=True).tz_convert(
    "America/New_York")
big_underlying_data["1_mo_avg"] = big_underlying_data["c"].rolling(window=20).mean()
big_underlying_data["3_mo_avg"] = big_underlying_data["c"].rolling(window=60).mean()
big_underlying_data['regime'] = big_underlying_data.apply(lambda row: 1 if (row['c'] > row['1_mo_avg']) else 0, axis=1)

trend_regime = big_underlying_data['regime'].iloc[-1]

ticker = "I:SPX"
index_ticker = "I:VIX1D"
options_ticker = "SPX"

trade_list = []

real_trading_dates = calendar.schedule(start_date=(datetime.today() - timedelta(days=10)),
                                       end_date=(datetime.today())).index.strftime("%Y-%m-%d").values

today = real_trading_dates[-1]

while 1:

    try:

        underlying_data = pd.json_normalize(requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{today}/{today}?adjusted=true&sort=asc&limit=50000&apiKey={polygon_api_key}").json()[
                                                "results"]).set_index("t")
        underlying_data.index = pd.to_datetime(underlying_data.index, unit="ms", utc=True).tz_convert(
            "America/New_York")

        index_data = pd.json_normalize(requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{index_ticker}/range/1/minute/{today}/{today}?adjusted=true&sort=asc&limit=50000&apiKey={polygon_api_key}").json()[
                                           "results"]).set_index("t")
        index_data.index = pd.to_datetime(index_data.index, unit="ms", utc=True).tz_convert("America/New_York")

        index_price = index_data[index_data.index.time >= pd.Timestamp("09:35").time()]["c"].iloc[0]
        price = underlying_data[underlying_data.index.time >= pd.Timestamp("09:35").time()]["c"].iloc[0]

        expected_move = (round((index_price / np.sqrt(252)), 2) / 100) * .50

        lower_price = round(price - (price * expected_move))
        upper_price = round(price + (price * expected_move))

        exp_date = date

        minute_timestamp = (pd.to_datetime(today).tz_localize("America/New_York") + timedelta(
            hours=pd.Timestamp("09:35").time().hour, minutes=pd.Timestamp("09:35").time().minute))
        quote_timestamp = minute_timestamp.value
        minute_after_timestamp = (pd.to_datetime(today).tz_localize("America/New_York") + timedelta(
            hours=pd.Timestamp("09:36").time().hour, minutes=pd.Timestamp("09:36").time().minute))
        quote_minute_after_timestamp = minute_after_timestamp.value

        if trend_regime == 0:

            side = "Call"

            valid_calls = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/reference/options/contracts?underlying_ticker={options_ticker}&contract_type=call&as_of={today}&expiration_date={exp_date}&limit=1000&apiKey={polygon_api_key}").json()[
                                                "results"])
            valid_calls = valid_calls[valid_calls["ticker"].str.contains("SPXW")].copy()
            valid_calls["days_to_exp"] = (pd.to_datetime(valid_calls["expiration_date"]) - pd.to_datetime(date)).dt.days
            valid_calls["distance_from_price"] = abs(valid_calls["strike_price"] - price)

            otm_calls = valid_calls[valid_calls["strike_price"] >= upper_price]

            short_call = otm_calls.iloc[[0]]
            long_call = otm_calls.iloc[[1]]

            short_strike = short_call["strike_price"].iloc[0]
            long_strike = long_call["strike_price"].iloc[0]

            short_call_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{short_call['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={quote_minute_after_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                      "results"]).set_index("sip_timestamp")
            short_call_quotes.index = pd.to_datetime(short_call_quotes.index, unit="ns", utc=True).tz_convert(
                "America/New_York")
            short_call_quote = short_call_quotes.median(numeric_only=True).to_frame().copy().T
            short_call_quote["mid_price"] = (short_call_quote["bid_price"] + short_call_quote["ask_price"]) / 2

            long_call_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{long_call['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={quote_minute_after_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                     "results"]).set_index("sip_timestamp")
            long_call_quotes.index = pd.to_datetime(long_call_quotes.index, unit="ns", utc=True).tz_convert(
                "America/New_York")
            long_call_quote = long_call_quotes.median(numeric_only=True).to_frame().copy().T
            long_call_quote["mid_price"] = (long_call_quote["bid_price"] + long_call_quote["ask_price"]) / 2

            spread_value = short_call_quote["mid_price"].iloc[0] - long_call_quote["mid_price"].iloc[0]

            underlying_data["distance_from_short_strike"] = round(
                ((short_strike - underlying_data["c"]) / underlying_data["c"].iloc[0]) * 100, 2)

            cost = spread_value

            updated_short_call_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{short_call['ticker'].iloc[0]}?order=desc&limit=100&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                              "results"]).set_index("sip_timestamp")
            updated_short_call_quotes.index = pd.to_datetime(updated_short_call_quotes.index, unit="ns",
                                                             utc=True).tz_convert("America/New_York")
            updated_short_call_quotes["mid_price"] = (updated_short_call_quotes["bid_price"] +
                                                      updated_short_call_quotes["ask_price"]) / 2

            updated_long_call_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{long_call['ticker'].iloc[0]}?order=desc&limit=100&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                             "results"]).set_index("sip_timestamp")
            updated_long_call_quotes.index = pd.to_datetime(updated_long_call_quotes.index, unit="ns",
                                                            utc=True).tz_convert("America/New_York")
            updated_long_call_quotes["mid_price"] = (updated_long_call_quotes["bid_price"] + updated_long_call_quotes[
                "ask_price"]) / 2

            updated_spread_value = updated_short_call_quotes["mid_price"].iloc[0] - \
                                   updated_long_call_quotes["mid_price"].iloc[0]

            gross_pnl = cost - updated_spread_value
            gross_pnl_percent = round((gross_pnl / cost) * 100, 2)

            print(
                f"Live PnL: ${round(gross_pnl * 100, 2)} | {gross_pnl_percent}% | {updated_short_call_quotes.index[0].strftime('%H:%M')}")
            print(
                f"Side: {side} | Short Strike: {short_strike} | Long Strike: {long_strike} | % Away from strike: {underlying_data['distance_from_short_strike'].iloc[-1]}%")
            time.sleep(10)

        elif trend_regime == 1:

            side = "Put"

            valid_puts = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/reference/options/contracts?underlying_ticker={options_ticker}&contract_type=put&as_of={today}&expiration_date={exp_date}&limit=1000&apiKey={polygon_api_key}").json()[
                                               "results"])
            valid_puts = valid_puts[valid_puts["ticker"].str.contains("SPXW")].copy()
            valid_puts["days_to_exp"] = (pd.to_datetime(valid_puts["expiration_date"]) - pd.to_datetime(date)).dt.days
            valid_puts["distance_from_price"] = abs(price - valid_puts["strike_price"])

            otm_puts = valid_puts[valid_puts["strike_price"] <= lower_price].sort_values("distance_from_price",
                                                                                         ascending=True)

            short_put = otm_puts.iloc[[0]]
            long_put = otm_puts.iloc[[1]]

            short_strike = short_put["strike_price"].iloc[0]
            long_strike = long_put["strike_price"].iloc[0]

            short_put_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{short_put['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={quote_minute_after_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                     "results"]).set_index("sip_timestamp")
            short_put_quotes.index = pd.to_datetime(short_put_quotes.index, unit="ns", utc=True).tz_convert(
                "America/New_York")
            short_put_quote = short_put_quotes.median(numeric_only=True).to_frame().copy().T
            short_put_quote["mid_price"] = (short_put_quote["bid_price"] + short_put_quote["ask_price"]) / 2

            long_put_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{long_put['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={quote_minute_after_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                    "results"]).set_index("sip_timestamp")
            long_put_quotes.index = pd.to_datetime(long_put_quotes.index, unit="ns", utc=True).tz_convert(
                "America/New_York")
            long_put_quote = long_put_quotes.median(numeric_only=True).to_frame().copy().T
            long_put_quote["mid_price"] = (long_put_quote["bid_price"] + long_put_quote["ask_price"]) / 2

            spread_value = short_put_quote["mid_price"].iloc[0] - long_put_quote["mid_price"].iloc[0]

            underlying_data["distance_from_short_strike"] = round(
                ((underlying_data["c"] - short_strike) / short_strike) * 100, 2)

            cost = spread_value

            updated_short_put_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{short_put['ticker'].iloc[0]}?order=desc&limit=100&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                             "results"]).set_index("sip_timestamp")
            updated_short_put_quotes.index = pd.to_datetime(updated_short_put_quotes.index, unit="ns",
                                                            utc=True).tz_convert("America/New_York")
            updated_short_put_quotes["mid_price"] = (updated_short_put_quotes["bid_price"] + updated_short_put_quotes[
                "ask_price"]) / 2

            updated_long_put_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{long_put['ticker'].iloc[0]}?order=desc&limit=100&sort=timestamp&apiKey={polygon_api_key}").json()[
                                                            "results"]).set_index("sip_timestamp")
            updated_long_put_quotes.index = pd.to_datetime(updated_long_put_quotes.index, unit="ns",
                                                           utc=True).tz_convert("America/New_York")
            updated_long_put_quotes["mid_price"] = (updated_long_put_quotes["bid_price"] + updated_long_put_quotes[
                "ask_price"]) / 2

            updated_spread_value = updated_short_put_quotes["mid_price"].iloc[0] - \
                                   updated_long_put_quotes["mid_price"].iloc[0]

            gross_pnl = cost - updated_spread_value
            gross_pnl_percent = round((gross_pnl / cost) * 100, 2)

            print(
                f"\nLive PnL: ${round(gross_pnl * 100, 2)} | {gross_pnl_percent}% | {updated_short_put_quotes.index[0].strftime('%H:%M')}")
            print(
                f"Side: {side} | Short Strike: {short_strike} | Long Strike: {long_strike} | % Away from strike: {underlying_data['distance_from_short_strike'].iloc[-1]}%")

            time.sleep(10)

    except Exception as data_error:
        print(data_error)
        continue
