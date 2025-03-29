"""
Modified in 2025 to include ML models for direction prediction and caching system
Original author: Quant Galore
Modified by: Claude
"""

import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from pandas_market_calendars import get_calendar
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
import os
from pathlib import Path
import sys
import re
import seaborn as sns
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.naive_bayes import GaussianNB
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report
import xgboost as xgb

# Add the project root directory to Python path
project_root = Path(__file__).resolve().parents[2]  # Go up 2 levels from src/backtest to reach project root
sys.path.append(str(project_root))

from config import settings

# Strategy parameters
polygon_api_key = settings.POLYGON.API_KEY
calendar = get_calendar("NYSE")
ticker = "I:SPX"
index_ticker = "I:VIX1D"
options_ticker = "SPX"
etf_ticker = "SPY"
trade_time = "09:35"
move_adjustment = .5
spread_width = 1

def sanitize_ticker(ticker):
    """Convert ticker to a safe filename by replacing invalid characters"""
    return re.sub(r'[<>:"/\\|?*]', '_', ticker)

def get_cached_data(ticker, date, use_cache=True, data_type="aggs", interval="1/minute"):
    """Get cached data for a specific date, or fetch from API if not cached"""
    # Use the exact directory names as they exist
    ticker_map = {
        'SPY': 'SPY',
        'I:VIX': 'I_VIX1D',
        'I:SPX': 'I_SPX'
    }
    
    dir_name = ticker_map.get(ticker, ticker)
    cache_dir = os.path.join(project_root, "data", dir_name)
    os.makedirs(cache_dir, exist_ok=True)
    
    # Match the actual file naming pattern in the data directory
    interval_str = interval.replace("/", "_")
    cache_file_day = os.path.join(cache_dir, f"{date}_1_day_aggs.csv")
    cache_file_minute = os.path.join(cache_dir, f"{date}_1_minute_aggs.csv")
    
    # Check for both day and minute files
    if use_cache:
        # First try to find the file with the matching interval
        if interval == "1/day" and os.path.exists(cache_file_day):
            cache_file = cache_file_day
        elif interval == "1/minute" and os.path.exists(cache_file_minute):
            cache_file = cache_file_minute
        # If the specific interval file doesn't exist, try the other one
        elif os.path.exists(cache_file_day):
            cache_file = cache_file_day
        elif os.path.exists(cache_file_minute):
            cache_file = cache_file_minute
        else:
            cache_file = None
            
        if cache_file and os.path.exists(cache_file):
            try:
                # Read CSV with explicit datetime parsing
                data = pd.read_csv(cache_file)
                if 't' in data.columns:
                    # Handle different timestamp formats
                    try:
                        # Try parsing as Unix timestamp first
                        data['t'] = pd.to_datetime(data['t'].astype(float), unit='ms')
                    except (ValueError, TypeError):
                        try:
                            # Try parsing as ISO format
                            data['t'] = pd.to_datetime(data['t'])
                        except (ValueError, TypeError):
                            print(f"Warning: Could not parse timestamps for {date}, using default parser")
                            data['t'] = pd.to_datetime(data['t'], format='mixed')
                    
                    data.set_index('t', inplace=True)
                    
                    # Handle timezone
                    if data.index.tz is None:
                        data.index = data.index.tz_localize('UTC')
                    data.index = data.index.tz_convert('America/New_York')
                
                # Ensure we have all required columns
                required_columns = {'o', 'c', 'h', 'l'}
                if not all(col in data.columns for col in required_columns):
                    print(f"Missing required columns in cached data for {date}")
                    return None
                
                # Add missing columns with default values if necessary
                if 'v' not in data:
                    data['v'] = 0
                
                return data
                
            except Exception as e:
                print(f"Error reading cache for {ticker} on {date}: {str(e)}")
                return None
    
    # If not in cache or cache read failed, fetch from API
    try:
        # Convert date to datetime for API request
        dt = pd.to_datetime(date)
        from_ts = int(dt.replace(hour=9, minute=30).timestamp() * 1000)
        to_ts = int(dt.replace(hour=16).timestamp() * 1000)
        
        # Construct API URL - fixed format for Polygon API
        api_ticker = ticker.replace('I:', '')  # Remove I: prefix for API call
        url = f"https://api.polygon.io/v2/aggs/ticker/{api_ticker}/range/{interval}/{date}/{date}?adjusted=true&sort=asc&limit=50000&apiKey={polygon_api_key}"
        response = requests.get(url)
        
        if response.status_code != 200:
            print(f"API request failed for {ticker} on {date}: {response.text}")
            return None
        
        results = response.json().get("results", [])
        if not results:
            print(f"No results found for {ticker} on {date}")
            return None
        
        # Create DataFrame with proper timezone handling
        df = pd.DataFrame(results)
        df['t'] = pd.to_datetime(df['t'], unit='ms', utc=True)
        df.set_index('t', inplace=True)
        df.index = df.index.tz_convert('America/New_York')
        
        # Filter for market hours
        df = df.between_time('09:30', '16:00')
        
        # Save to cache if requested
        if use_cache:
            os.makedirs(os.path.dirname(cache_file_minute), exist_ok=True)
            # Save with the correct naming pattern based on interval
            if interval == "1/day":
                df.to_csv(cache_file_day)
            else:
                df.to_csv(cache_file_minute)
        
        return df
        
    except Exception as e:
        print(f"Error fetching data for {date}: {str(e)}")
        return None

def get_cached_data_range(ticker, start_date, end_date, data_type="aggs", interval="1/minute"):
    """Get cached data for a date range, returns a dict of dates->data and missing dates"""
    safe_ticker = sanitize_ticker(ticker)
    cache_dir = os.path.join(project_root, "data", safe_ticker)
    
    # Convert dates to datetime for comparison
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    
    # Get all trading dates in the range
    all_dates = []
    current_dt = start_dt
    while current_dt <= end_dt:
        if current_dt.weekday() < 5:  # Monday to Friday
            all_dates.append(current_dt.strftime("%Y-%m-%d"))
        current_dt += pd.Timedelta(days=1)
    
    # Check which dates we already have cached
    cached_data = {}
    missing_dates = []
    
    for date in all_dates:
        data = get_cached_data(ticker, date, use_cache=True, data_type=data_type, interval=interval)
        if data is not None:
            cached_data[date] = data
        else:
            missing_dates.append(date)
    
    return cached_data, missing_dates

def fetch_missing_data(ticker, dates, data_type="aggs", interval="1/minute", use_cache=True):
    """Fetch only missing data for given dates"""
    fetched_data = {}
    
    for date in tqdm(dates, desc="Fetching missing data"):
        try:
            data = get_cached_data(ticker, date, data_type, interval, use_cache)
            if data is not None:
                fetched_data[date] = data
        except Exception as e:
            print(f"Error fetching data for {date}: {str(e)}")
    
    return fetched_data

def create_features(data, lookback_periods=[5, 10, 20, 60]):
    """Create features for ML models"""
    if data is None or len(data) == 0:
        return None
        
    try:
        df = data.copy()
        
        # Ensure we have the required columns
        required_columns = {'o', 'h', 'l', 'c'}
        if not all(col in df.columns for col in required_columns):
            print(f"Missing required columns for feature creation. Found: {df.columns}")
            return None
        
        # Calculate returns
        df['returns'] = df['c'].pct_change()
        
        # Moving averages
        for period in lookback_periods:
            df[f'ma_{period}'] = df['c'].rolling(window=period).mean()
            df[f'std_{period}'] = df['c'].rolling(window=period).std()
        
        # RSI
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi_14'] = 100 - (100 / (1 + rs))
        
        # Momentum
        df['momentum_5'] = df['c'].diff(5)
        
        # ATR
        df['tr'] = np.maximum(
            df['h'] - df['l'],
            np.maximum(
                abs(df['h'] - df['c'].shift(1)),
                abs(df['l'] - df['c'].shift(1))
            )
        )
        df['atr'] = df['tr'].rolling(window=14).mean()
        
        # Target variable (next day's direction)
        df['target'] = np.where(df['c'].shift(-1) > df['c'], 1, 0)
        
        # Drop NaN values
        df = df.dropna()
        
        # Select features for ML
        feature_columns = ['returns'] + \
                         [f'ma_{p}' for p in lookback_periods] + \
                         [f'std_{p}' for p in lookback_periods] + \
                         ['rsi_14', 'momentum_5', 'atr', 'target']
        
        return df[feature_columns]
        
    except Exception as e:
        print(f"Error creating features: {str(e)}")
        return None

def train_ml_models(features, target):
    """Train multiple ML models and return all models"""
    # Split data
    X_train, X_test, y_train, y_test = train_test_split(features, target, test_size=0.2, random_state=42)
    
    # Scale features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)
    
    # Train models
    models = {}
    
    # Gaussian Naive Bayes
    try:
        gnb = GaussianNB()
        gnb.fit(X_train_scaled, y_train)
        models['GaussianNB'] = {
            'model': gnb,
            'scaler': scaler,
            'accuracy': accuracy_score(y_test, gnb.predict(X_test_scaled))
        }
    except Exception as e:
        print(f"Error training GaussianNB: {str(e)}")
    
    # Random Forest
    try:
        rf = RandomForestClassifier(n_estimators=100, random_state=42)
        rf.fit(X_train_scaled, y_train)
        models['RandomForest'] = {
            'model': rf,
            'scaler': scaler,
            'accuracy': accuracy_score(y_test, rf.predict(X_test_scaled))
        }
    except Exception as e:
        print(f"Error training RandomForest: {str(e)}")
    
    # Logistic Regression
    try:
        lr = LogisticRegression(random_state=42, max_iter=1000)
        lr.fit(X_train_scaled, y_train)
        models['LogisticRegression'] = {
            'model': lr,
            'scaler': scaler,
            'accuracy': accuracy_score(y_test, lr.predict(X_test_scaled))
        }
    except Exception as e:
        print(f"Error training LogisticRegression: {str(e)}")
    
    # Print model performance
    for name, model_dict in models.items():
        print(f"{name} Accuracy: {model_dict['accuracy']:.4f}")
    
    return models

def fetch_data_parallel(date, prior_date, use_cache=True):
    """Fetch all required data for a given date in parallel"""
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            'prior_day_underlying': executor.submit(get_cached_data, ticker, prior_date, "aggs", "1/day", use_cache),
            'etf_underlying': executor.submit(get_cached_data, etf_ticker, date, "aggs", "1/minute", use_cache),
            'underlying': executor.submit(get_cached_data, ticker, date, "aggs", "1/minute", use_cache),
            'index': executor.submit(get_cached_data, index_ticker, date, "aggs", "1/minute", use_cache)
        }
        
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as e:
                print(f"Error fetching {name} data: {e}")
                return None
                
    return results

def run_ml_options_strategy(start_date, end_date, initial_capital=3000, use_cache=True):
    """Run the options trading strategy using multiple ML models"""
    # Get trading dates
    trading_dates = calendar.schedule(start_date=start_date, end_date=end_date).index.strftime("%Y-%m-%d").values
    
    # Initialize progress bar
    pbar = tqdm(total=len(trading_dates))
    
    # Initialize trade lists for each model
    trade_lists = {
        'GaussianNB': [],
        'RandomForest': [],
        'LogisticRegression': []
    }
    
    # Get historical data for ML training (60 days before start_date)
    lookback_start = (pd.to_datetime(start_date) - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    print("Checking cached historical data...")
    
    # Get SPY data for the lookback period
    spy_data, missing_spy = get_cached_data_range('SPY', lookback_start, start_date)
    if missing_spy:
        print(f"Fetching {len(missing_spy)} missing SPY dates...")
        spy_data.update(fetch_missing_data('SPY', missing_spy))
    
    # Get VIX data for the lookback period
    vix_data, missing_vix = get_cached_data_range('I_VIX1D', lookback_start, start_date)
    if missing_vix:
        print(f"Fetching {len(missing_vix)} missing VIX dates...")
        vix_data.update(fetch_missing_data('I_VIX1D', missing_vix))
    
    # Get SPX data for the lookback period
    spx_data, missing_spx = get_cached_data_range('I_SPX', lookback_start, start_date)
    if missing_spx:
        print(f"Fetching {len(missing_spx)} missing SPX dates...")
        spx_data.update(fetch_missing_data('I_SPX', missing_spx))
    
    # Prepare initial ML training data
    historical_data = []
    for date in pd.date_range(lookback_start, start_date, freq='B'):
        date_str = date.strftime("%Y-%m-%d")
        if date_str in spy_data:
            data_dict = {
                'underlying': spy_data.get(date_str),
                'vix': vix_data.get(date_str, None),  # Allow None
                'index': spx_data.get(date_str, None)  # Allow None
            }
            historical_data.append(data_dict)
    
    if len(historical_data) < 30:  # Require minimum amount of training data
        print("Insufficient historical data for ML training")
        return None, None
    
    # Create features for ML training
    initial_features = create_features(pd.concat([d['underlying'] for d in historical_data]))
    
    if len(initial_features) < 30:  # Require minimum amount of training data
        print("Insufficient features for ML training")
        return None, None
    
    # Train initial models
    X = initial_features.drop(['target'], axis=1)
    y = initial_features['target']
    models = train_ml_models(X, y)
    
    if not models or len(models) < 3:  # Ensure all models are trained
        print("Failed to train all ML models")
        return None, None
    
    # Main trading loop
    for date in trading_dates:
        pbar.update(1)
        pbar.set_description(f"Processing {date}")
        
        try:
            # Get prior date
            prior_date = (pd.to_datetime(date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            
            # Fetch data for current and prior date
            current_data = fetch_data_parallel(date, prior_date, use_cache)
            
            # Skip if we don't have required data
            if not current_data or 'underlying' not in current_data or current_data['underlying'] is None:
                pbar.write(f"Missing underlying data for {date}")
                continue
                
            if 'prior_day_underlying' not in current_data or current_data['prior_day_underlying'] is None:
                pbar.write(f"Missing prior day data for {date}")
                continue
            
            # Create features
            features = create_features(current_data['underlying'])
            if features is None or len(features) == 0:
                pbar.write(f"Could not create features for {date}")
                continue
            
            # Get predictions from each model
            current_features = features.drop(['target'], axis=1).iloc[-1:]
            
            for model_name, model_dict in models.items():
                try:
                    # Scale features using the model's scaler
                    scaled_features = model_dict['scaler'].transform(current_features)
                    direction = model_dict['model'].predict(scaled_features)[0]
                    
                    # Execute trade based on model prediction
                    trade_data = execute_trade(date, direction, current_data)
                    
                    if trade_data:
                        trade_data['model_used'] = model_name
                        trade_lists[model_name].append(trade_data)
                    else:
                        pbar.write(f"No trade executed for {model_name} on {date}")
                        
                except Exception as e:
                    pbar.write(f"Error with {model_name} on {date}: {str(e)}")
                    continue
            
            # Update models with new data
            if len(features) > 0:
                X = features.drop(['target'], axis=1)
                y = features['target']
                updated_models = train_ml_models(X, y)
                
                # Only update if all models trained successfully
                if updated_models and len(updated_models) == 3:
                    models = updated_models
                
        except Exception as e:
            pbar.write(f"Error on date {date}: {str(e)}")
            continue
            
    pbar.close()
    
    # Combine all trades with their respective model labels
    all_trades = []
    for model_name, trades in trade_lists.items():
        if trades:
            all_trades.extend(trades)
            print(f"Total trades for {model_name}: {len(trades)}")
    
    return all_trades, models

def execute_trade(date, direction, data):
    """Execute a single trade based on the predicted direction"""
    try:
        # Call spread logic
        if direction == 0:
            valid_calls = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/reference/options/contracts?underlying_ticker={options_ticker}&contract_type=call&as_of={date}&expiration_date={date}&limit=1000&apiKey={polygon_api_key}").json()["results"])
            
            if len(valid_calls) == 0:
                print(f"No valid call options found for {date}")
                return None
            
            valid_calls = valid_calls[valid_calls["ticker"].str.contains("SPXW")].copy()
            if len(valid_calls) == 0:
                print(f"No SPXW call options found for {date}")
                return None
            
            valid_calls["days_to_exp"] = (pd.to_datetime(valid_calls["expiration_date"]) - pd.to_datetime(date)).dt.days
            valid_calls["distance_from_price"] = abs(valid_calls["strike_price"] - data['underlying']["c"].iloc[0])

            otm_calls = valid_calls[valid_calls["strike_price"] >= data['underlying']["c"].iloc[0]]
            if len(otm_calls) < spread_width + 1:
                print(f"Not enough OTM calls found for spread on {date}")
                return None
            
            short_call = otm_calls.iloc[[0]]
            long_call = otm_calls.iloc[[spread_width]]
            short_strike = short_call["strike_price"].iloc[0]

            # Get quotes
            minute_timestamp = (pd.to_datetime(date).tz_localize("America/New_York") +
                              timedelta(hours=pd.Timestamp(trade_time).time().hour,
                                      minutes=pd.Timestamp(trade_time).time().minute))
            quote_timestamp = minute_timestamp.value
            close_timestamp = (pd.to_datetime(date).tz_localize("America/New_York") +
                             timedelta(hours=16, minutes=0)).value

            short_call_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{short_call['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={close_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()["results"]).set_index("sip_timestamp")
            short_call_quotes.index = pd.to_datetime(short_call_quotes.index, unit="ns", utc=True).tz_convert("America/New_York")
            short_call_quotes["mid_price"] = round((short_call_quotes["bid_price"] + short_call_quotes["ask_price"]) / 2, 2)
            short_call_quotes = short_call_quotes[short_call_quotes.index.strftime("%Y-%m-%d %H:%M") <= minute_timestamp.strftime("%Y-%m-%d %H:%M")].copy()

            short_call_quote = short_call_quotes.median(numeric_only=True).to_frame().copy().T
            short_call_quote["t"] = minute_timestamp.strftime("%Y-%m-%d %H:%M")

            long_call_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{long_call['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={close_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()["results"]).set_index("sip_timestamp")
            long_call_quotes.index = pd.to_datetime(long_call_quotes.index, unit="ns", utc=True).tz_convert("America/New_York")
            long_call_quotes["mid_price"] = round((long_call_quotes["bid_price"] + long_call_quotes["ask_price"]) / 2, 2)
            long_call_quotes = long_call_quotes[long_call_quotes.index.strftime("%Y-%m-%d %H:%M") <= minute_timestamp.strftime("%Y-%m-%d %H:%M")].copy()

            long_call_quote = long_call_quotes.median(numeric_only=True).to_frame().copy().T
            long_call_quote["t"] = minute_timestamp.strftime("%Y-%m-%d %H:%M")

            spread = pd.concat([short_call_quote.add_prefix("short_call_"), long_call_quote.add_prefix("long_call_")], axis=1).dropna()
            spread["spread_value"] = spread["short_call_mid_price"] - spread["long_call_mid_price"]
            cost = spread["spread_value"].iloc[0]
            max_loss = abs(short_call["strike_price"].iloc[0] - long_call["strike_price"].iloc[0]) - cost

        # Put spread logic
        elif direction == 1:
            valid_puts = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/reference/options/contracts?underlying_ticker={options_ticker}&contract_type=put&as_of={date}&expiration_date={date}&limit=1000&apiKey={polygon_api_key}").json()["results"])
            
            if len(valid_puts) == 0:
                print(f"No valid put options found for {date}")
                return None
            
            valid_puts = valid_puts[valid_puts["ticker"].str.contains("SPXW")].copy()
            if len(valid_puts) == 0:
                print(f"No SPXW put options found for {date}")
                return None
            
            valid_puts["days_to_exp"] = (pd.to_datetime(valid_puts["expiration_date"]) - pd.to_datetime(date)).dt.days
            valid_puts["distance_from_price"] = abs(data['underlying']["c"].iloc[0] - valid_puts["strike_price"])

            otm_puts = valid_puts[valid_puts["strike_price"] <= data['underlying']["c"].iloc[0]].sort_values("distance_from_price", ascending=True)
            if len(otm_puts) < spread_width + 1:
                print(f"Not enough OTM puts found for spread on {date}")
                return None
            
            short_put = otm_puts.iloc[[0]]
            long_put = otm_puts.iloc[[spread_width]]
            short_strike = short_put["strike_price"].iloc[0]

            # Get quotes
            minute_timestamp = (pd.to_datetime(date).tz_localize("America/New_York") +
                              timedelta(hours=pd.Timestamp(trade_time).time().hour,
                                      minutes=pd.Timestamp(trade_time).time().minute))
            quote_timestamp = minute_timestamp.value
            close_timestamp = (pd.to_datetime(date).tz_localize("America/New_York") +
                             timedelta(hours=16, minutes=0)).value

            short_put_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{short_put['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={close_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()["results"]).set_index("sip_timestamp")
            short_put_quotes.index = pd.to_datetime(short_put_quotes.index, unit="ns", utc=True).tz_convert("America/New_York")
            short_put_quotes["mid_price"] = round((short_put_quotes["bid_price"] + short_put_quotes["ask_price"]) / 2, 2)
            short_put_quotes = short_put_quotes[short_put_quotes.index.strftime("%Y-%m-%d %H:%M") <= minute_timestamp.strftime("%Y-%m-%d %H:%M")].copy()

            short_put_quote = short_put_quotes.median(numeric_only=True).to_frame().copy().T
            short_put_quote["t"] = minute_timestamp.strftime("%Y-%m-%d %H:%M")

            long_put_quotes = pd.json_normalize(requests.get(
                f"https://api.polygon.io/v3/quotes/{long_put['ticker'].iloc[0]}?timestamp.gte={quote_timestamp}&timestamp.lt={close_timestamp}&order=asc&limit=5000&sort=timestamp&apiKey={polygon_api_key}").json()["results"]).set_index("sip_timestamp")
            long_put_quotes.index = pd.to_datetime(long_put_quotes.index, unit="ns", utc=True).tz_convert("America/New_York")
            long_put_quotes["mid_price"] = round((long_put_quotes["bid_price"] + long_put_quotes["ask_price"]) / 2, 2)
            long_put_quotes = long_put_quotes[long_put_quotes.index.strftime("%Y-%m-%d %H:%M") <= minute_timestamp.strftime("%Y-%m-%d %H:%M")].copy()

            long_put_quote = long_put_quotes.median(numeric_only=True).to_frame().copy().T
            long_put_quote["t"] = minute_timestamp.strftime("%Y-%m-%d %H:%M")

            spread = pd.concat([short_put_quote.add_prefix("short_put_"), long_put_quote.add_prefix("long_put_")], axis=1).dropna()
            spread["spread_value"] = spread["short_put_mid_price"] - spread["long_put_mid_price"]
            cost = spread["spread_value"].iloc[0]
            max_loss = abs(short_put["strike_price"].iloc[0] - long_put["strike_price"].iloc[0]) - cost

        # Calculate P&L
        if direction == 1:
            settlement = data['underlying']["c"].iloc[-1] - short_strike
            if settlement > 0:
                settlement = 0
                final_pnl = cost
            else:
                final_pnl = settlement + cost
        elif direction == 0:
            settlement = short_strike - data['underlying']["c"].iloc[-1]
            if settlement > 0:
                settlement = 0
                final_pnl = cost
            else:
                final_pnl = settlement + cost

        gross_pnl = np.maximum(final_pnl, max_loss * -1)
        gross_pnl_percent = round((gross_pnl / cost) * 100, 2)

        # Store trade data
        trade_data = {
            "date": date,
            "direction": direction,
            "pnl": gross_pnl,  
            "pnl_percent": gross_pnl_percent,
            "ticker": ticker,
            "short_strike": short_strike,
            "closing_value": data['underlying']["c"].iloc[-1],
            "overnight_move": round(((data['underlying']["c"].iloc[0] - data['prior_day_underlying']["c"].iloc[0]) / data['prior_day_underlying']["c"].iloc[0]) * 100, 2),
            "total_cost": cost * 100  # Options contracts are for 100 shares
        }

        return trade_data
    except Exception as e:
        print(f"Error executing trade on {date}: {str(e)}")
        return None

def calculate_performance_metrics(trades_df, strategy_name="Strategy"):
    """Calculate comprehensive performance metrics for a given set of trades"""
    if len(trades_df) == 0:
        print(f"No trades for {strategy_name}")
        return None, None
        
    # Create a copy to avoid SettingWithCopyWarning
    trades_df = trades_df.copy()
    
    # Calculate returns properly handling edge cases
    trades_df['returns'] = trades_df['pnl'] / abs(trades_df['total_cost'])  # Use absolute cost
    trades_df['cumulative_returns'] = (1 + trades_df['returns']).cumprod()
    
    # Basic metrics
    total_trades = len(trades_df)
    winning_trades = len(trades_df[trades_df['pnl'] > 0])
    losing_trades = len(trades_df[trades_df['pnl'] < 0])
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
    
    # Performance metrics with safety checks
    try:
        total_return = (trades_df['cumulative_returns'].iloc[-1] - 1) * 100
        # Cap extreme values
        total_return = np.clip(total_return, -99.99, 1000)
        
        # Calculate annualized metrics only if we have enough data
        if len(trades_df) >= 5:  # Require at least 5 trades
            annual_factor = 252 / len(trades_df)
            annual_return = ((1 + total_return/100) ** annual_factor - 1) * 100
            volatility = trades_df['returns'].std() * np.sqrt(252) * 100
            sharpe_ratio = annual_return / volatility if volatility != 0 else 0
        else:
            annual_return = total_return
            volatility = trades_df['returns'].std() * 100
            sharpe_ratio = 0
    except Exception:
        total_return = 0
        annual_return = 0
        volatility = 0
        sharpe_ratio = 0
    
    # Calculate drawdown
    rolling_max = trades_df['cumulative_returns'].expanding().max()
    drawdown = (trades_df['cumulative_returns'] - rolling_max) / rolling_max * 100
    max_drawdown = drawdown.min()
    
    # Win/Loss metrics
    avg_win = trades_df[trades_df['pnl'] > 0]['pnl'].mean() if winning_trades > 0 else 0
    avg_loss = trades_df[trades_df['pnl'] < 0]['pnl'].mean() if losing_trades > 0 else 0
    expected_value = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
    
    # Monthly analysis using ME (month end) instead of deprecated M
    monthly_returns = trades_df['returns'].resample('ME').sum() * 100
    
    metrics = {
        'Strategy': strategy_name,
        'Total Return (%)': total_return,
        'Annualized Return (%)': annual_return,
        'Annualized Volatility (%)': volatility,
        'Sharpe Ratio': sharpe_ratio,
        'Maximum Drawdown (%)': max_drawdown,
        'Total Trades': total_trades,
        'Win Rate (%)': win_rate,
        'Average Win ($)': avg_win,
        'Average Loss ($)': avg_loss,
        'Expected Value ($)': expected_value,
        'Best Month (%)': monthly_returns.max() if len(monthly_returns) > 0 else 0,
        'Worst Month (%)': monthly_returns.min() if len(monthly_returns) > 0 else 0,
        'Average Monthly Return (%)': monthly_returns.mean() if len(monthly_returns) > 0 else 0,
        'Monthly Return Std (%)': monthly_returns.std() if len(monthly_returns) > 0 else 0
    }
    
    return metrics, trades_df['cumulative_returns']

def analyze_strategy_results(trade_list):
    """Analyze and visualize strategy results with comprehensive metrics"""
    if not trade_list:
        print("No trades to analyze")
        return

    # Convert to DataFrame and ensure proper date handling
    all_trades = pd.DataFrame(trade_list)
    all_trades['date'] = pd.to_datetime(all_trades['date'])
    all_trades.set_index('date', inplace=True)
    all_trades = all_trades.sort_index()
    
    # Calculate returns for each model
    all_trades['returns'] = all_trades['pnl'] / abs(all_trades['total_cost'])
    
    # Separate trades by model type
    model_trades = {
        'GaussianNB': all_trades[all_trades['model_used'] == 'GaussianNB'].copy(),
        'RandomForest': all_trades[all_trades['model_used'] == 'RandomForest'].copy(),
        'LogisticRegression': all_trades[all_trades['model_used'] == 'LogisticRegression'].copy()
    }
    
    # Calculate metrics for each model
    metrics_list = []
    equity_curves = {}
    
    for model_name, trades in model_trades.items():
        if len(trades) > 0:
            # Calculate cumulative returns for each model separately
            trades['cumulative_returns'] = (1 + trades['returns']).cumprod()
            metrics, equity_curve = calculate_performance_metrics(trades, model_name)
            if metrics:
                metrics_list.append(metrics)
                equity_curves[model_name] = equity_curve
    
    if not metrics_list:
        print("No valid metrics could be calculated")
        return
    
    # Create performance comparison DataFrame
    performance_comparison = pd.DataFrame(metrics_list).set_index('Strategy')
    
    # Visualizations
    fig = plt.figure(figsize=(20, 15))
    gs = fig.add_gridspec(3, 2)
    
    # 1. Equity Curves by Model
    ax1 = fig.add_subplot(gs[0, :])
    for model_name, curve in equity_curves.items():
        ax1.plot(curve.index, curve.values, label=model_name, linewidth=2)
    ax1.set_title('ML Model Comparison - Equity Curves')
    ax1.set_ylabel('Cumulative Returns')
    ax1.grid(True)
    ax1.legend()
    
    # 2. Monthly Returns Heatmap (for each model)
    ax2 = fig.add_subplot(gs[1, 0])
    model_monthly_returns = {}
    for model_name, trades in model_trades.items():
        if len(trades) > 0:
            monthly_returns = trades['returns'].resample('ME').sum() * 100
            model_monthly_returns[model_name] = monthly_returns
    
    if model_monthly_returns:
        monthly_returns_df = pd.DataFrame(model_monthly_returns)
        sns.heatmap(monthly_returns_df, annot=True, fmt='.1f', cmap='RdYlGn', center=0, ax=ax2)
        ax2.set_title('Monthly Returns by Model (%)')
        ax2.set_xlabel('Model')
        ax2.set_ylabel('Month')
    
    # 3. Return Distribution by Model
    ax3 = fig.add_subplot(gs[1, 1])
    for model_name, trades in model_trades.items():
        if len(trades) > 0:
            returns = np.clip(trades['returns'] * 100, -100, 100)  # Clip extreme values
            sns.kdeplot(data=returns, label=model_name, ax=ax3)
    ax3.axvline(0, color='r', linestyle='--')
    ax3.set_title('Return Distribution by Model')
    ax3.set_xlabel('Returns (%) - Clipped at ±100%')
    ax3.set_ylabel('Density')
    ax3.legend()
    
    # 4. Drawdown Analysis by Model
    ax4 = fig.add_subplot(gs[2, 0])
    for model_name, curve in equity_curves.items():
        rolling_max = curve.expanding().max()
        drawdown = (curve - rolling_max) / rolling_max * 100
        ax4.plot(drawdown.index, drawdown.values, label=model_name)
    ax4.set_title('Drawdown Analysis by Model')
    ax4.set_ylabel('Drawdown (%)')
    ax4.grid(True)
    ax4.legend()
    
    # 5. Model Comparison Metrics
    ax5 = fig.add_subplot(gs[2, 1])
    metrics_to_plot = ['Win Rate (%)', 'Sharpe Ratio', 'Maximum Drawdown (%)']
    comparison_data = performance_comparison[metrics_to_plot].copy()
    comparison_data.plot(kind='bar', ax=ax5)
    ax5.set_title('Model Comparison - Key Metrics')
    ax5.set_ylabel('Value')
    ax5.grid(True)
    plt.xticks(rotation=45)
    
    plt.tight_layout()
    plt.show()
    
    # Print detailed performance metrics for each model
    print("\n=== Detailed Performance Metrics by Model ===")
    print("\nPerformance Comparison:")
    with pd.option_context('display.float_format', '{:.2f}'.format):
        print(performance_comparison.to_string())
    
    return all_trades

if __name__ == "__main__":
    try:
        # Set date range
        start_date = "2023-05-01"
        end_date = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        # Run strategy with caching enabled
        trade_list, model = run_ml_options_strategy(start_date, end_date, initial_capital=3000, use_cache=True)
        
        # Analyze results
        analyze_strategy_results(trade_list)
        
    except Exception as e:
        print(f"Error during backtest execution: {str(e)}")
        import traceback
        traceback.print_exc()  # Add full traceback for better error diagnosis
        raise