import requests
import pandas as pd
import numpy as np
import time
import json
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


def get_session_token(environment: Literal['production', 'sandbox']):
    with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
        session_token = db.get('session_token')
        token_expiry = db.get('token_expiry')

        if session_token and token_expiry and datetime.now() < token_expiry:
            print("Using existing valid session token.")
            return session_token

        # If no valid token, create a new SANDBOX session
        environment = environment

        if environment == 'sandbox':
            url = settings.TASTY_SANDBOX_BASE_URL
            url += "/sessions"

            payload = {
                "login": settings.TASTY_SANDBOX.USERNAME,
                "password": settings.TASTY_SANDBOX.PASSWORD
            }

            headers = {
                "Content-Type": "application/json"
            }

            response = requests.post(url, data=json.dumps(payload), headers=headers)

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
        else:
            url = settings.TASTY_PRODUCTION_BASE_URL
            url += "/sessions"

            payload = {
                "login": settings.TASTY_PRODUCTION.USERNAME,
                "password": settings.TASTY_PRODUCTION.PASSWORD
            }

            headers = {
                "Content-Type": "application/json"
            }

            response = requests.post(url, data=json.dumps(payload), headers=headers)

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


# Example usage
if token := get_session_token(environment='sandbox'):
    print(f"Session token: {token}")
