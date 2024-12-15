import asyncio
import json
import websockets
import shelve
import requests
import pandas as pd
import numpy as np

from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from loguru import logger
from typing import Dict, Any, Literal, Optional, List, Union
from dynaconf import Dynaconf
from pathlib import Path
from dataclasses import dataclass
from abc import ABC, abstractmethod

settings = Dynaconf(settings_files=['settings.json', '.secrets.json'])

logger.add('logs/account_manager_{time}.log', rotation='1 day')

EnvironmentType = Literal['sandbox', 'production']

@dataclass
class MarketDataConfig:
    """Configuration for market data streams"""
    symbols: List[str]
    channels: List[str]
    update_interval: float
    batch_size: int = 100
    max_retries: int = 3
    retry_delay: float = 5.0

class MarketDataValidator:
    """Validates incoming market data"""
    
    @staticmethod
    def validate_price(price: float) -> bool:
        return isinstance(price, (int, float)) and price > 0

    @staticmethod
    def validate_timestamp(timestamp: Union[int, str]) -> bool:
        try:
            if isinstance(timestamp, str):
                datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            else:
                datetime.fromtimestamp(timestamp / 1000)
            return True
        except ValueError:
            return False

    @staticmethod
    def validate_symbol(symbol: str) -> bool:
        return isinstance(symbol, str) and len(symbol) > 0

class MarketDataProcessor:
    """Processes and normalizes market data"""
    
    def __init__(self):
        self.mongo_client = AsyncIOMotorClient(settings.MONGO_URI)
        self.db = self.mongo_client[settings.MONGO_DATABASE]
        self.market_data_collection = self.db.market_data

    async def process_trade(self, trade_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process trade data and store in standardized format"""
        try:
            if not all([
                MarketDataValidator.validate_symbol(trade_data.get('symbol', '')),
                MarketDataValidator.validate_price(trade_data.get('price', 0)),
                MarketDataValidator.validate_timestamp(trade_data.get('timestamp', 0))
            ]):
                logger.warning(f"Invalid trade data received: {trade_data}")
                return None

            processed_data = {
                'symbol': trade_data['symbol'],
                'price': float(trade_data['price']),
                'timestamp': datetime.fromtimestamp(
                    trade_data['timestamp'] / 1000 if isinstance(trade_data['timestamp'], int)
                    else datetime.fromisoformat(trade_data['timestamp'].replace('Z', '+00:00')).timestamp()
                ),
                'source': trade_data.get('source', 'unknown'),
                'processed_at': datetime.now(timezone.utc)
            }

            await self.market_data_collection.insert_one(processed_data)
            return processed_data

        except Exception as e:
            logger.error(f"Error processing trade data: {e}")
            return None

class DataStreamBase(ABC):
    """Base class for data streaming implementations"""
    
    def __init__(self, config: MarketDataConfig):
        self.config = config
        self.processor = MarketDataProcessor()
        self.is_connected = False
        self.retry_count = 0

    @abstractmethod
    async def connect(self):
        """Establish connection to data source"""
        pass

    @abstractmethod
    async def disconnect(self):
        """Disconnect from data source"""
        pass

    @abstractmethod
    async def subscribe(self, symbols: List[str], channels: List[str]):
        """Subscribe to market data"""
        pass

    async def handle_connection_error(self):
        """Handle connection errors with exponential backoff"""
        if self.retry_count < self.config.max_retries:
            wait_time = self.config.retry_delay * (2 ** self.retry_count)
            logger.warning(f"Connection lost. Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
            self.retry_count += 1
            return True
        return False

class PolygonDataStream(DataStreamBase):
    """Polygon.io WebSocket implementation"""
    
    def __init__(self, config: MarketDataConfig):
        super().__init__(config)
        self.ws_url = "wss://socket.polygon.io/stocks"
        self.api_key = settings.POLYGON.API_KEY
        self.websocket = None

    async def connect(self):
        """Connect to Polygon.io WebSocket"""
        try:
            self.websocket = await websockets.connect(self.ws_url)
            auth_message = {"action": "auth", "params": self.api_key}
            await self.websocket.send(json.dumps(auth_message))
            response = await self.websocket.recv()
            
            if json.loads(response)["status"] == "connected":
                self.is_connected = True
                self.retry_count = 0
                logger.success("Connected to Polygon.io WebSocket")
                return True
            
            logger.error(f"Failed to authenticate with Polygon.io: {response}")
            return False

        except Exception as e:
            logger.error(f"Error connecting to Polygon.io: {e}")
            return await self.handle_connection_error()

    async def disconnect(self):
        """Disconnect from Polygon.io WebSocket"""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info("Disconnected from Polygon.io WebSocket")

    async def subscribe(self, symbols: List[str], channels: List[str]):
        """Subscribe to Polygon.io market data channels"""
        if not self.is_connected:
            logger.error("Not connected to Polygon.io WebSocket")
            return False

        try:
            subscribe_message = {
                "action": "subscribe",
                "params": f"{','.join(channels)}.{','.join(symbols)}"
            }
            await self.websocket.send(json.dumps(subscribe_message))
            logger.info(f"Subscribed to Polygon.io channels: {channels} for symbols: {symbols}")
            return True

        except Exception as e:
            logger.error(f"Error subscribing to Polygon.io channels: {e}")
            return False

    async def process_messages(self):
        """Process incoming messages from Polygon.io"""
        while self.is_connected:
            try:
                message = await self.websocket.recv()
                data = json.loads(message)
                
                if data[0]['ev'] == 'T':  # Trade event
                    processed_data = await self.processor.process_trade({
                        'symbol': data[0]['sym'],
                        'price': data[0]['p'],
                        'timestamp': data[0]['t'],
                        'source': 'polygon'
                    })
                    if processed_data:
                        logger.debug(f"Processed trade: {processed_data}")

            except websockets.ConnectionClosed:
                logger.warning("Polygon.io WebSocket connection closed")
                self.is_connected = False
                break
            except Exception as e:
                logger.error(f"Error processing Polygon.io message: {e}")

class TastyTradeDataStream(DataStreamBase):
    """TastyTrade WebSocket implementation"""
    
    def __init__(self, config: MarketDataConfig):
        super().__init__(config)
        self.websocket_url = (
            "wss://streamer.cert.tastyworks.com" 
            if settings.ENVIRONMENT == 'sandbox'
            else "wss://streamer.tastyworks.com"
        )
        self.websocket = None
        self.session_token = None
        self.heartbeat_interval = 15

    async def get_session_token(self) -> Optional[str]:
        """Get or refresh session token"""
        try:
            token_data = await asyncio.to_thread(self._read_token_from_shelf)
            if token_data and token_data[1] > datetime.now():
                return token_data[0]

            # Token expired or not found, get new token
            if settings.ENVIRONMENT == 'sandbox':
                url = f"{settings.TASTY_SANDBOX_BASE_URL}/sessions"
                credentials = {
                    "login": settings.TASTY_SANDBOX.USERNAME,
                    "password": settings.TASTY_SANDBOX.PASSWORD
                }
            else:
                url = f"{settings.TASTY_PRODUCTION_BASE_URL}/sessions"
                credentials = {
                    "login": settings.TASTY_PRODUCTION.USERNAME,
                    "password": settings.TASTY_PRODUCTION.PASSWORD
                }

            response = await asyncio.to_thread(
                requests.post,
                url,
                json=credentials,
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 201:
                token = response.json()['data']['session-token']
                expiry = datetime.now() + timedelta(hours=24)
                await asyncio.to_thread(self._write_token_to_shelf, token, expiry)
                return token

            logger.error(f"Failed to get session token: {response.text}")
            return None

        except Exception as e:
            logger.error(f"Error getting session token: {e}")
            return None

    def _read_token_from_shelf(self):
        """Read token from shelf storage"""
        with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
            return db.get('session_token'), db.get('token_expiry')

    def _write_token_to_shelf(self, token: str, expiry: datetime):
        """Write token to shelf storage"""
        with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
            db['session_token'] = token
            db['token_expiry'] = expiry

    async def connect(self):
        """Connect to TastyTrade WebSocket"""
        try:
            self.session_token = await self.get_session_token()
            if not self.session_token:
                return False

            self.websocket = await websockets.connect(self.websocket_url)
            connect_message = {
                "action": "connect",
                "value": [settings.ACCOUNT_NUMBER],
                "auth-token": self.session_token
            }
            await self.websocket.send(json.dumps(connect_message))
            response = json.loads(await self.websocket.recv())

            if response.get("status") == "ok":
                self.is_connected = True
                self.retry_count = 0
                logger.success("Connected to TastyTrade WebSocket")
                asyncio.create_task(self._heartbeat())
                return True

            logger.error(f"Failed to connect to TastyTrade: {response}")
            return False

        except Exception as e:
            logger.error(f"Error connecting to TastyTrade: {e}")
            return await self.handle_connection_error()

    async def _heartbeat(self):
        """Send periodic heartbeats"""
        while self.is_connected:
            try:
                heartbeat_message = {
                    "action": "heartbeat",
                    "auth-token": self.session_token
                }
                await self.websocket.send(json.dumps(heartbeat_message))
                await asyncio.sleep(self.heartbeat_interval)
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                self.is_connected = False
                break

    async def disconnect(self):
        """Disconnect from TastyTrade WebSocket"""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info("Disconnected from TastyTrade WebSocket")

    async def subscribe(self, symbols: List[str], channels: List[str]):
        """Subscribe to TastyTrade market data channels"""
        if not self.is_connected:
            logger.error("Not connected to TastyTrade WebSocket")
            return False

        try:
            subscribe_message = {
                "action": "subscribe",
                "value": [{"symbol": symbol, "type": channel} 
                         for symbol in symbols 
                         for channel in channels]
            }
            await self.websocket.send(json.dumps(subscribe_message))
            logger.info(f"Subscribed to TastyTrade channels: {channels} for symbols: {symbols}")
            return True

        except Exception as e:
            logger.error(f"Error subscribing to TastyTrade channels: {e}")
            return False

    async def process_messages(self):
        """Process incoming messages from TastyTrade"""
        while self.is_connected:
            try:
                message = await self.websocket.recv()
                data = json.loads(message)
                
                if 'data' in data and 'quote' in data['data']:
                    quote = data['data']['quote']
                    processed_data = await self.processor.process_trade({
                        'symbol': quote['symbol'],
                        'price': (quote['bid'] + quote['ask']) / 2,
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'source': 'tastytrade'
                    })
                    if processed_data:
                        logger.debug(f"Processed quote: {processed_data}")

            except websockets.ConnectionClosed:
                logger.warning("TastyTrade WebSocket connection closed")
                self.is_connected = False
                break
            except Exception as e:
                logger.error(f"Error processing TastyTrade message: {e}")

class MarketDataManager:
    """Manages multiple data streams and coordinates data flow"""
    
    def __init__(self, config: MarketDataConfig):
        self.config = config
        self.polygon_stream = PolygonDataStream(config)
        self.tastytrade_stream = TastyTradeDataStream(config)
        self.streams = [self.polygon_stream, self.tastytrade_stream]

    async def start(self):
        """Start all data streams"""
        for stream in self.streams:
            if await stream.connect():
                await stream.subscribe(self.config.symbols, self.config.channels)
                asyncio.create_task(stream.process_messages())
            else:
                logger.error(f"Failed to start {stream.__class__.__name__}")

    async def stop(self):
        """Stop all data streams"""
        for stream in self.streams:
            await stream.disconnect()

async def main():
    """Main entry point"""
    config = MarketDataConfig(
        symbols=["SPX", "VIX"],
        channels=["trades", "quotes"],
        update_interval=1.0
    )
    
    manager = MarketDataManager(config)
    
    try:
        await manager.start()
        # Keep the program running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        await manager.stop()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await manager.stop()

if __name__ == "__main__":
    asyncio.run(main())
