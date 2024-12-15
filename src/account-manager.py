import asyncio
import json
import websockets
import shelve
import requests
import pandas as pd
import numpy as np
from scipy.stats import norm

from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from loguru import logger
from typing import Dict, Any, Literal, Optional, List, Union, Type, Callable
from dynaconf import Dynaconf
from pathlib import Path
from dataclasses import dataclass
from abc import ABC, abstractmethod
from enum import Enum
import inspect

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

class PluginState(Enum):
    """Plugin lifecycle states"""
    REGISTERED = "registered"
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"

class PluginMetadata:
    """Metadata for plugin tracking and management"""
    def __init__(self, name: str, version: str, dependencies: List[str]):
        self.name = name
        self.version = version
        self.dependencies = dependencies
        self.state = PluginState.REGISTERED
        self.error = None
        self.start_time = None
        self.stop_time = None

class PluginEvent:
    """Event object for plugin communication"""
    def __init__(self, event_type: str, data: Any, source: str):
        self.event_type = event_type
        self.data = data
        self.source = source
        self.timestamp = datetime.now(timezone.utc)

class PluginBase(ABC):
    """Base class for all plugins"""
    
    def __init__(self, name: str, version: str, dependencies: List[str] = None):
        self.metadata = PluginMetadata(name, version, dependencies or [])
        self._event_handlers: Dict[str, List[Callable]] = {}

    @abstractmethod
    async def initialize(self, context: Dict[str, Any]) -> bool:
        """Initialize plugin with context"""
        pass

    @abstractmethod
    async def start(self) -> bool:
        """Start plugin operation"""
        pass

    @abstractmethod
    async def stop(self) -> bool:
        """Stop plugin operation"""
        pass

    def register_event_handler(self, event_type: str, handler: Callable[[PluginEvent], None]):
        """Register an event handler"""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    async def handle_event(self, event: PluginEvent):
        """Handle incoming events"""
        if event.event_type in self._event_handlers:
            for handler in self._event_handlers[event.event_type]:
                try:
                    await handler(event)
                except Exception as e:
                    logger.error(f"Error in event handler {handler.__name__}: {e}")

class PluginManager:
    """Manages plugin lifecycle and communication"""
    
    def __init__(self):
        self._plugins: Dict[str, PluginBase] = {}
        self._plugin_dependencies: Dict[str, List[str]] = {}
        self._context: Dict[str, Any] = {}

    def register_plugin(self, plugin: PluginBase) -> bool:
        """Register a new plugin"""
        try:
            # Validate plugin
            if not isinstance(plugin, PluginBase):
                raise ValueError(f"Plugin must inherit from PluginBase: {plugin}")
            
            name = plugin.metadata.name
            if name in self._plugins:
                raise ValueError(f"Plugin {name} already registered")

            # Validate dependencies
            for dep in plugin.metadata.dependencies:
                if dep not in self._plugins:
                    raise ValueError(f"Dependency {dep} not found for plugin {name}")

            self._plugins[name] = plugin
            self._plugin_dependencies[name] = plugin.metadata.dependencies
            plugin.metadata.state = PluginState.REGISTERED
            logger.info(f"Registered plugin: {name} v{plugin.metadata.version}")
            return True

        except Exception as e:
            logger.error(f"Error registering plugin: {e}")
            return False

    def deregister_plugin(self, name: str) -> bool:
        """Deregister a plugin"""
        try:
            if name not in self._plugins:
                raise ValueError(f"Plugin {name} not found")

            # Check if any other plugins depend on this one
            for plugin_name, deps in self._plugin_dependencies.items():
                if name in deps:
                    raise ValueError(f"Cannot deregister {name}, required by {plugin_name}")

            plugin = self._plugins[name]
            if plugin.metadata.state == PluginState.RUNNING:
                raise ValueError(f"Cannot deregister running plugin {name}")

            del self._plugins[name]
            del self._plugin_dependencies[name]
            logger.info(f"Deregistered plugin: {name}")
            return True

        except Exception as e:
            logger.error(f"Error deregistering plugin: {e}")
            return False

    async def initialize_plugin(self, name: str) -> bool:
        """Initialize a registered plugin"""
        try:
            plugin = self._plugins.get(name)
            if not plugin:
                raise ValueError(f"Plugin {name} not found")

            if plugin.metadata.state != PluginState.REGISTERED:
                raise ValueError(f"Plugin {name} in invalid state: {plugin.metadata.state}")

            # Initialize dependencies first
            for dep in plugin.metadata.dependencies:
                if not await self.initialize_plugin(dep):
                    raise ValueError(f"Failed to initialize dependency {dep}")

            if await plugin.initialize(self._context):
                plugin.metadata.state = PluginState.INITIALIZED
                logger.info(f"Initialized plugin: {name}")
                return True

            raise ValueError(f"Plugin {name} initialization failed")

        except Exception as e:
            logger.error(f"Error initializing plugin: {e}")
            plugin.metadata.state = PluginState.ERROR
            plugin.metadata.error = str(e)
            return False

    async def start_plugin(self, name: str) -> bool:
        """Start an initialized plugin"""
        try:
            plugin = self._plugins.get(name)
            if not plugin:
                raise ValueError(f"Plugin {name} not found")

            if plugin.metadata.state != PluginState.INITIALIZED:
                raise ValueError(f"Plugin {name} not initialized")

            # Start dependencies first
            for dep in plugin.metadata.dependencies:
                if not await self.start_plugin(dep):
                    raise ValueError(f"Failed to start dependency {dep}")

            if await plugin.start():
                plugin.metadata.state = PluginState.RUNNING
                plugin.metadata.start_time = datetime.now(timezone.utc)
                logger.info(f"Started plugin: {name}")
                return True

            raise ValueError(f"Plugin {name} failed to start")

        except Exception as e:
            logger.error(f"Error starting plugin: {e}")
            plugin.metadata.state = PluginState.ERROR
            plugin.metadata.error = str(e)
            return False

    async def stop_plugin(self, name: str) -> bool:
        """Stop a running plugin"""
        try:
            plugin = self._plugins.get(name)
            if not plugin:
                raise ValueError(f"Plugin {name} not found")

            if plugin.metadata.state != PluginState.RUNNING:
                raise ValueError(f"Plugin {name} not running")

            # Stop plugins that depend on this one first
            for dep_name, deps in self._plugin_dependencies.items():
                if name in deps and self._plugins[dep_name].metadata.state == PluginState.RUNNING:
                    if not await self.stop_plugin(dep_name):
                        raise ValueError(f"Failed to stop dependent plugin {dep_name}")

            if await plugin.stop():
                plugin.metadata.state = PluginState.STOPPED
                plugin.metadata.stop_time = datetime.now(timezone.utc)
                logger.info(f"Stopped plugin: {name}")
                return True

            raise ValueError(f"Plugin {name} failed to stop")

        except Exception as e:
            logger.error(f"Error stopping plugin: {e}")
            plugin.metadata.state = PluginState.ERROR
            plugin.metadata.error = str(e)
            return False

    async def emit_event(self, event: PluginEvent):
        """Emit an event to all plugins"""
        for plugin in self._plugins.values():
            if plugin.metadata.state == PluginState.RUNNING:
                await plugin.handle_event(event)

    def get_plugin_status(self, name: str) -> Optional[Dict[str, Any]]:
        """Get plugin status information"""
        plugin = self._plugins.get(name)
        if not plugin:
            return None

        return {
            "name": plugin.metadata.name,
            "version": plugin.metadata.version,
            "state": plugin.metadata.state.value,
            "dependencies": plugin.metadata.dependencies,
            "error": plugin.metadata.error,
            "start_time": plugin.metadata.start_time,
            "stop_time": plugin.metadata.stop_time
        }

    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all registered plugins and their status"""
        return [self.get_plugin_status(name) for name in self._plugins.keys()]

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
        self.plugin_manager = PluginManager()  # Add plugin manager

    async def start(self):
        """Start all data streams and initialize plugins"""
        # Start data streams
        for stream in self.streams:
            if await stream.connect():
                await stream.subscribe(self.config.symbols, self.config.channels)
                asyncio.create_task(stream.process_messages())
            else:
                logger.error(f"Failed to start {stream.__class__.__name__}")

        # Initialize and start all plugins
        for plugin_name in self.plugin_manager._plugins.keys():
            if await self.plugin_manager.initialize_plugin(plugin_name):
                await self.plugin_manager.start_plugin(plugin_name)

    async def stop(self):
        """Stop all data streams and plugins"""
        # Stop all plugins
        for plugin_name in self.plugin_manager._plugins.keys():
            await self.plugin_manager.stop_plugin(plugin_name)

        # Stop data streams
        for stream in self.streams:
            await stream.disconnect()

    async def handle_market_data(self, data: Dict[str, Any]):
        """Handle market data and emit to plugins"""
        event = PluginEvent("market_data", data, "market_data_manager")
        await self.plugin_manager.emit_event(event)

@dataclass
class OptionGreeks:
    """Container for option Greeks calculations"""
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    timestamp: datetime

@dataclass
class GreeksEvent:
    """Event class for option Greeks following dxFeed protocol"""
    eventType: str = "Greeks"
    eventSymbol: str  # Option symbol
    eventTime: int    # UTC timestamp in milliseconds
    sequence: int     # Event sequence number
    
    # Greeks values
    delta: float      # Rate of change of option price with respect to underlying price
    gamma: float      # Rate of change of delta with respect to underlying price
    theta: float      # Rate of change of option price with respect to time
    vega: float       # Rate of change of option price with respect to volatility
    rho: float        # Rate of change of option price with respect to interest rate
    
    # Additional Greeks metadata
    impliedVolatility: float  # Current implied volatility
    theorPrice: float         # Theoretical price of the option
    
    @classmethod
    def create(cls, symbol: str, greeks: OptionGreeks, theo_price: float, iv: float) -> 'GreeksEvent':
        """Create a Greeks event from calculated values"""
        return cls(
            eventSymbol=symbol,
            eventTime=int(datetime.now(timezone.utc).timestamp() * 1000),
            sequence=int(time.time() * 1000),  # Use timestamp as sequence
            delta=greeks.delta,
            gamma=greeks.gamma,
            theta=greeks.theta,
            vega=greeks.vega,
            rho=greeks.rho,
            impliedVolatility=iv,
            theorPrice=theo_price
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary format for transmission"""
        return {
            "eventType": self.eventType,
            "eventSymbol": self.eventSymbol,
            "eventTime": self.eventTime,
            "sequence": self.sequence,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "rho": self.rho,
            "impliedVolatility": self.impliedVolatility,
            "theorPrice": self.theorPrice
        }

class GreeksCalculator:
    """Handles option Greeks calculations using Black-Scholes model"""
    
    @staticmethod
    def calculate_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
        """Calculate d1 component of Black-Scholes formula"""
        try:
            return (np.log(S/K) + (r + sigma**2/2)*T) / (sigma*np.sqrt(T))
        except Exception as e:
            logger.error(f"Error calculating d1: {e}")
            raise ValueError("Invalid parameters for d1 calculation")

    @staticmethod
    def calculate_d2(d1: float, sigma: float, T: float) -> float:
        """Calculate d2 component of Black-Scholes formula"""
        try:
            return d1 - sigma*np.sqrt(T)
        except Exception as e:
            logger.error(f"Error calculating d2: {e}")
            raise ValueError("Invalid parameters for d2 calculation")

    @staticmethod
    def calculate_greeks(
        S: float,      # Current stock price
        K: float,      # Strike price
        T: float,      # Time to expiration (in years)
        r: float,      # Risk-free rate
        sigma: float,  # Volatility
        is_call: bool  # True for call, False for put
    ) -> OptionGreeks:
        """Calculate all Greeks for an option position"""
        try:
            # Input validation
            if any(x <= 0 for x in [S, K, T, sigma]) or r < 0:
                raise ValueError("Invalid input parameters")

            # Calculate d1 and d2
            d1 = GreeksCalculator.calculate_d1(S, K, T, r, sigma)
            d2 = GreeksCalculator.calculate_d2(d1, sigma, T)

            # Calculate N(d1) and N(d2)
            Nd1 = norm.cdf(d1 if is_call else -d1)
            Nd2 = norm.cdf(d2 if is_call else -d2)
            
            # Calculate option price
            if is_call:
                delta = Nd1
                theta = (-S*sigma*np.exp(-d1**2/2)/(2*np.sqrt(2*np.pi*T)) - 
                        r*K*np.exp(-r*T)*Nd2)/365
            else:
                delta = Nd1 - 1
                theta = (-S*sigma*np.exp(-d1**2/2)/(2*np.sqrt(2*np.pi*T)) + 
                        r*K*np.exp(-r*T)*(1 - Nd2))/365

            # Common Greeks calculations
            gamma = np.exp(-d1**2/2)/(S*sigma*np.sqrt(2*np.pi*T))
            vega = S*np.sqrt(T)*np.exp(-d1**2/2)/np.sqrt(2*np.pi)/100
            rho = K*T*np.exp(-r*T)*Nd2/100 if is_call else -K*T*np.exp(-r*T)*(1-Nd2)/100

            return OptionGreeks(
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                rho=rho,
                timestamp=datetime.now(timezone.utc)
            )

        except Exception as e:
            logger.error(f"Error calculating Greeks: {e}")
            raise

class GreeksEventPlugin(PluginBase):
    """Plugin for real-time Greeks calculations and event handling"""
    
    def __init__(self):
        super().__init__(
            name="greeks_calculator",
            version="1.0.0",
            dependencies=[]
        )
        self.calculator = GreeksCalculator()
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.market_data: Dict[str, Dict[str, Any]] = {}
        self.last_calculation: Dict[str, OptionGreeks] = {}
        self.calculation_frequency = timedelta(seconds=1)  # Adjust as needed
        self._last_update = {}

    async def initialize(self, context: Dict[str, Any]) -> bool:
        """Initialize the Greeks calculator plugin"""
        try:
            logger.info("Initializing Greeks calculator plugin")
            # Register event handlers
            self.register_event_handler("market_data", self._handle_market_data)
            self.register_event_handler("position_update", self._handle_position_update)
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Greeks calculator: {e}")
            return False

    async def start(self) -> bool:
        """Start the Greeks calculator plugin"""
        try:
            logger.info("Starting Greeks calculator plugin")
            # Start periodic calculations
            asyncio.create_task(self._periodic_calculations())
            return True
        except Exception as e:
            logger.error(f"Failed to start Greeks calculator: {e}")
            return False

    async def stop(self) -> bool:
        """Stop the Greeks calculator plugin"""
        try:
            logger.info("Stopping Greeks calculator plugin")
            # Clean up resources
            self.positions.clear()
            self.market_data.clear()
            self.last_calculation.clear()
            return True
        except Exception as e:
            logger.error(f"Failed to stop Greeks calculator: {e}")
            return False

    async def _handle_market_data(self, event: PluginEvent):
        """Handle incoming market data updates"""
        try:
            data = event.data
            symbol = data.get("symbol")
            if not symbol:
                return

            self.market_data[symbol] = {
                "price": data.get("price"),
                "timestamp": event.timestamp
            }

            # Trigger Greeks recalculation if needed
            await self._calculate_greeks_for_symbol(symbol)

        except Exception as e:
            logger.error(f"Error handling market data in Greeks calculator: {e}")

    async def _handle_position_update(self, event: PluginEvent):
        """Handle position updates"""
        try:
            position = event.data
            symbol = position.get("symbol")
            if not symbol:
                return

            self.positions[symbol] = {
                "strike": position.get("strike"),
                "expiry": position.get("expiry"),
                "is_call": position.get("is_call"),
                "quantity": position.get("quantity"),
                "timestamp": event.timestamp
            }

            # Trigger Greeks recalculation
            await self._calculate_greeks_for_symbol(symbol)

        except Exception as e:
            logger.error(f"Error handling position update in Greeks calculator: {e}")

    async def _calculate_greeks_for_symbol(self, symbol: str):
        """Calculate Greeks for a specific symbol"""
        try:
            # Check if we have all required data
            if (symbol not in self.positions or 
                symbol not in self.market_data or 
                not self._should_calculate(symbol)):
                return

            position = self.positions[symbol]
            market_data = self.market_data[symbol]

            # Extract parameters
            S = market_data["price"]
            K = position["strike"]
            T = (position["expiry"] - datetime.now(timezone.utc)).total_seconds() / (365.25 * 24 * 3600)
            r = 0.05  # Risk-free rate (could be made dynamic)
            sigma = 0.20  # Implied volatility (could be made dynamic)
            is_call = position["is_call"]

            # Calculate Greeks
            greeks = self.calculator.calculate_greeks(S, K, T, r, sigma, is_call)
            
            # Scale by position size
            quantity = position["quantity"]
            greeks.delta *= quantity
            greeks.gamma *= quantity
            greeks.theta *= quantity
            greeks.vega *= quantity
            greeks.rho *= quantity

            # Store calculation
            self.last_calculation[symbol] = greeks
            self._last_update[symbol] = datetime.now(timezone.utc)

            # Emit Greeks update event
            await self._emit_greeks_update(symbol, greeks)

        except Exception as e:
            logger.error(f"Error calculating Greeks for {symbol}: {e}")

    def _should_calculate(self, symbol: str) -> bool:
        """Determine if we should recalculate Greeks based on frequency"""
        last_update = self._last_update.get(symbol)
        if not last_update:
            return True
        return datetime.now(timezone.utc) - last_update >= self.calculation_frequency

    async def _periodic_calculations(self):
        """Periodically recalculate Greeks for all positions"""
        while True:
            try:
                for symbol in self.positions.keys():
                    await self._calculate_greeks_for_symbol(symbol)
                await asyncio.sleep(self.calculation_frequency.total_seconds())
            except Exception as e:
                logger.error(f"Error in periodic Greeks calculations: {e}")
                await asyncio.sleep(1)

    async def _emit_greeks_update(self, symbol: str, greeks: OptionGreeks):
        """Emit Greeks update event following dxFeed protocol"""
        try:
            # Calculate theoretical price (simplified for example)
            theo_price = 0.0  # This should be calculated based on your pricing model
            implied_vol = 0.20  # This should come from your volatility model
            
            # Create Greeks event
            greeks_event = GreeksEvent.create(
                symbol=symbol,
                greeks=greeks,
                theo_price=theo_price,
                iv=implied_vol
            )
            
            # Create plugin event
            event = PluginEvent(
                event_type="greeks_update",
                data=greeks_event.to_dict(),
                source="greeks_calculator"
            )
            
            await self.handle_event(event)
            
        except Exception as e:
            logger.error(f"Error emitting Greeks update: {e}")

class DxFeedGreeksPlugin(PluginBase):
    """Plugin for streaming Greeks data from dxFeed WebSocket API"""
    
    def __init__(self):
        super().__init__(
            name="dxfeed_greeks",
            version="1.0.0",
            dependencies=[]
        )
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.latest_greeks: Dict[str, Dict[str, Any]] = {}
        self.subscribed_symbols: Set[str] = set()
        self.ws_uri = "wss://demo.dxfeed.com/dxlink-ws"
        self._running = False

    async def initialize(self, context: Dict[str, Any]) -> bool:
        """Initialize connection to dxFeed WebSocket"""
        try:
            logger.info("Initializing dxFeed Greeks streaming plugin")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize dxFeed Greeks plugin: {e}")
            return False

    async def start(self) -> bool:
        """Start the WebSocket connection and message handling"""
        try:
            logger.info("Starting dxFeed Greeks streaming")
            self._running = True
            asyncio.create_task(self._websocket_handler())
            return True
        except Exception as e:
            logger.error(f"Failed to start dxFeed Greeks streaming: {e}")
            return False

    async def stop(self) -> bool:
        """Stop the WebSocket connection"""
        try:
            logger.info("Stopping dxFeed Greeks streaming")
            self._running = False
            if self.websocket:
                await self.websocket.close()
            return True
        except Exception as e:
            logger.error(f"Failed to stop dxFeed Greeks streaming: {e}")
            return False

    async def _websocket_handler(self):
        """Handle WebSocket connection and message processing"""
        while self._running:
            try:
                async with websockets.connect(self.ws_uri) as websocket:
                    self.websocket = websocket
                    
                    # Send initial subscription for Greeks events
                    await self._subscribe_to_greeks()
                    
                    # Handle incoming messages
                    async for message in websocket:
                        await self._handle_message(message)
                        
            except websockets.ConnectionClosed:
                logger.warning("dxFeed WebSocket connection closed, attempting to reconnect...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error in dxFeed WebSocket handler: {e}")
                await asyncio.sleep(5)

    async def _subscribe_to_greeks(self):
        """Send subscription request for Greeks events"""
        if not self.websocket:
            return
            
        try:
            # Format subscription message according to dxFeed protocol
            subscription = {
                "type": "Subscribe",
                "events": ["Greeks"],
                "symbols": list(self.subscribed_symbols)
            }
            await self.websocket.send(json.dumps(subscription))
        except Exception as e:
            logger.error(f"Error subscribing to Greeks events: {e}")

    async def _handle_message(self, message: str):
        """Process incoming WebSocket messages"""
        try:
            data = json.loads(message)
            
            # Handle Greeks events
            if "Greeks" in data.get("events", []):
                for event in data["events"]["Greeks"]:
                    symbol = event.get("eventSymbol")
                    if symbol:
                        # Store latest Greeks data
                        self.latest_greeks[symbol] = event
                        
                        # Emit plugin event with Greeks data
                        plugin_event = PluginEvent(
                            event_type="greeks_update",
                            data=event,
                            source="dxfeed_greeks"
                        )
                        await self.handle_event(plugin_event)
                        
        except Exception as e:
            logger.error(f"Error processing dxFeed message: {e}")

    async def subscribe_symbol(self, symbol: str):
        """Subscribe to Greeks events for a specific option symbol"""
        try:
            self.subscribed_symbols.add(symbol)
            if self.websocket:
                await self._subscribe_to_greeks()
        except Exception as e:
            logger.error(f"Error subscribing to symbol {symbol}: {e}")

    async def unsubscribe_symbol(self, symbol: str):
        """Unsubscribe from Greeks events for a specific symbol"""
        try:
            self.subscribed_symbols.remove(symbol)
            if self.websocket:
                await self._subscribe_to_greeks()
        except Exception as e:
            logger.error(f"Error unsubscribing from symbol {symbol}: {e}")

    def get_latest_greeks(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get the most recent Greeks data for a symbol"""
        return self.latest_greeks.get(symbol)

async def main():
    """Main entry point"""
    config = MarketDataConfig(
        symbols=["SPX", "VIX"],
        channels=["trades", "quotes"],
        update_interval=1.0
    )
    
    manager = MarketDataManager(config)
    plugin_manager = manager.plugin_manager
    
    # Register Greeks calculator plugin
    plugin_manager.register_plugin(GreeksEventPlugin())
    
    # Register dxFeed Greeks plugin
    plugin_manager.register_plugin(DxFeedGreeksPlugin())
    
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
