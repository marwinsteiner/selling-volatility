import asyncio
import json
import websockets
import shelve
import requests

from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from loguru import logger
from typing import Dict, Any, Literal
from dynaconf import Dynaconf
from pathlib import Path

settings = Dynaconf(settings_files=['settings.json', '.secrets.json'])

logger.add('logs/account_manager_{time}.log', rotation='1 day')

EnvironmentType = Literal['sandbox', 'production']  # create a type alias


class AccountManager:
    def __init__(self):
        self.mongo_client = AsyncIOMotorClient(settings.MONGO_URI)
        self.db = self.mongo_client[settings.MONGO_DATABASE]
        self.positions_collection = self.db.positions
        self.trades_collection = self.db.trades
        self.current_positions: Dict[str, Any] = {}
        self.websocket_url = (
            "wss://streamer.cert.tastyworks.com")
        self.heartbeat_interval = 15  # seconds
        self.web_socket_session_id = None
        # ENVIRONMENT toggles between sandbox (testing) and production (live trading)
        self.ENVIRONMENT: EnvironmentType = 'sandbox'
        """
        May have to think about making the self.websocket_url attribute dynamic -- so we have some helper 
        functions in  helper module where we retrieve the balances, for example. When we retrieve the balances, 
        we also get the account number, so there would be no need to hard code this. Also, we would then determine 
        whether it's a production or sandbox environment dynamically. That would demand full abstraction, 
        meaning modifying production-tastytrade.py such that ENVIRONMENT is a settings.json config item/variable and 
        we use it as such from production-tastytrade.py alongside the account-manager.py. For now we hard code both 
        of these variables to their sandbox values.
        """

    async def connect_websocket(self):
        """Establish websocket connection with TastyTrade"""
        async with websockets.connect(self.websocket_url) as websocket:
            logger.info("WebSocket connection established")

            # Send initial connect message
            connect_message = {
                "action": "connect",
                "value": [settings.ACCOUNT_NUMBER],
                # TODO: change ACCOUNT_NUMBER such that it takes whatever account number is given back in the request
                #  for authentication. Means modify authentication to retrieve this and write to variable
                #  account_number.
                "auth-token": await self.get_session_token(environment=self.ENVIRONMENT),
                "request-id": 1
            }
            await websocket.send(json.dumps(connect_message))
            response = await websocket.recv()
            connect_response = json.loads(response)

            if connect_response.get("status") == "ok":
                self.web_socket_session_id = connect_response.get("web-socket-session-id")
                logger.success(f"Connected with session ID: {self.web_socket_session_id}")

                # Start heartbeat task
                heartbeat_task = asyncio.create_task(self.send_heartbeats(websocket))

                # Start message handling
                try:
                    await self.handle_messages(websocket)
                finally:
                    heartbeat_task.cancel()
            else:
                logger.error(f"Failed to connect: {connect_response}")

    async def send_heartbeats(self, websocket):
        """Send periodic heartbeats to maintain connection"""
        while True:
            try:
                heartbeat_message = {
                    "action": "heartbeat",
                    "auth-token": await self.get_session_token(environment=self.ENVIRONMENT),
                    "request-id": int(datetime.now().timestamp())
                }
                await websocket.send(json.dumps(heartbeat_message))
                response = await websocket.recv()
                heartbeat_response = json.loads(response)

                if heartbeat_response.get("status") != "ok":
                    logger.warning(f"Heartbeat failed: {heartbeat_response}")

                await asyncio.sleep(self.heartbeat_interval)
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                break

    async def handle_messages(self, websocket):
        """Process incoming websocket messages"""
        try:
            async for message in websocket:
                data = json.loads(message)
                message_type = data.get('type')

                if message_type == 'Order':
                    await self.process_order_update(data['data'])
                elif message_type == 'Position':
                    await self.update_positions(data['data'])
                elif message_type == 'Balance':
                    await self.update_balance(data['data'])

        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def process_order_update(self, data: Dict):
        """Process order updates and store in MongoDB"""
        try:
            order_doc = {
                'order_id': data.get('id'),
                'status': data.get('status'),
                'timestamp': datetime.now(timezone.utc),
                'raw_data': data,
                'strategy': 'spx_0dte_credit_spread',
                'account_number': data.get('account-number'),
            }

            await self.trades_collection.insert_one(order_doc)
            logger.info(f"Stored order update for order {data.get('id')}")

        except Exception as e:
            logger.error(f"Error processing order update: {e}")

    async def update_positions(self, data: Dict):
        """Update current positions based on position updates"""
        try:
            position_doc = {
                'account_number': data.get('account-number'),
                'positions': data,
                'last_updated': datetime.now(timezone.utc),
            }

            await self.positions_collection.update_one(
                {'account_number': data.get('account-number')},
                {'$set': position_doc},
                upsert=True
            )
            logger.info(f"Updated positions for account {data.get('account-number')}")

        except Exception as e:
            logger.error(f"Error updating positions: {e}")

    async def update_balance(self, data: Dict):
        """Update account balance information"""
        try:
            balance_doc = {
                'account_number': data.get('account-number'),
                'balance_data': data,
                'timestamp': datetime.now(timezone.utc),
            }

            await self.db.balances.update_one(
                {'account_number': data.get('account-number')},
                {'$set': balance_doc},
                upsert=True
            )
            logger.info(f"Updated balance for account {data.get('account-number')}")

        except Exception as e:
            logger.error(f"Error updating balance: {e}")

    async def get_session_token(self, environment: EnvironmentType) -> str:
        """
        Asynchronously get or generate a session token based on the environment.
  
        Args:
            environment (str): The environment type ('sandbox' or 'production').
  
        Returns:
            str: The session token if found or generated, None if the request fails.
        """
        # First try to get existing token from shelve - wrapped in to_thread since shelve is synchronous
        try:
            session_token, token_expiry = await asyncio.to_thread(
                self._read_token_from_shelf
            )

            # Check if we have a valid token that hasn't expired
            if session_token and token_expiry and datetime.now() < token_expiry:
                logger.success('Found existing session token.', extra={'session_token': session_token})
                logger.info(f'Existing session token will expire at {token_expiry}.')
                return session_token
        except Exception as e:
            logger.error(f"Error reading from shelf: {e}")

        # If we get here, we either don't have a token or it's expired
        logger.warning('Session token expired or invalid, generating new session token...')

        # Prepare request parameters based on environment
        if self.ENVIRONMENT == 'sandbox':
            url = f"{settings.TASTY_SANDBOX_BASE_URL}/sessions"
            logger.info(f'Using environment:{self} with base url: {url}')
            payload = {
                "login": settings.TASTY_SANDBOX.USERNAME,
                "password": settings.TASTY_SANDBOX.PASSWORD
            }
        else:
            url = f"{settings.TASTY_PRODUCTION_BASE_URL}/sessions"
            logger.info(f'Using environment:{self} with base url: {url}')
            payload = {
                "login": settings.TASTY_PRODUCTION.USERNAME,
                "password": settings.TASTY_PRODUCTION.PASSWORD
            }

        logger.debug('Generated payload.')
        headers = {"Content-Type": "application/json"}

        # Make HTTP request - wrapped in to_thread since requests is synchronous
        try:
            response = await asyncio.to_thread(
                requests.post,
                url,
                json=payload,
                headers=headers
            )
            logger.info(f'Posted request: {response}')

            if response.status_code == 201:
                logger.success(f'Response status code: {response.status_code}. Received session token.')
                data = response.json()
                new_session_token = data['data']['session-token']
                new_token_expiry = datetime.now() + timedelta(hours=24)
                logger.debug(f'Saved new session token expiring at: {new_token_expiry}.')

                # Store new token in shelve - wrapped in to_thread
                await asyncio.to_thread(
                    self._write_token_to_shelf,
                    new_session_token,
                    new_token_expiry
                )
                logger.success('Stored new session token and token expiry.')

                return new_session_token
            else:
                logger.error(f'Session token request failed with response code: {response.status_code}.')
                logger.debug(f'{response.text}')
                return None

        except Exception as e:
            logger.error(f'HTTP request failed: {str(e)}')
            return None

    def _read_token_from_shelf(self):
        """Helper function to read token from shelf synchronously"""
        with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
            return db.get('session_token'), db.get('token_expiry')

    def _write_token_to_shelf(self, token: str, expiry: datetime):
        """Helper function to write token to shelf synchronously"""
        with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
            db['session_token'] = token
            db['token_expiry'] = expiry


async def main():
    account_manager = AccountManager()
    while True:
        try:
            await account_manager.connect_websocket()
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            await asyncio.sleep(5)  # Wait before reconnecting


if __name__ == "__main__":
    asyncio.run(main())
