import sys
from pathlib import Path
project_root = str(Path(__file__).parent.parent)
sys.path.insert(0, project_root)
from config import settings
import websockets
import json
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Callable, Tuple
from dataclasses import dataclass
import shelve
from typing import Literal
from loguru import logger

# Set up a logging directory
log_dir = Path(r'C:\Users\marwi\PycharmProjects\selling-volatility\src\logs')
log_dir.mkdir(exist_ok=True)

# Create log file path with timestamp
log_file = log_dir / f"tastytrade_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# Configure logger to write to both console and file
logger.add(log_file, rotation="1 day")

EnvironmentType = Literal['sandbox', 'production']  # create a type alias

# ENVIRONMENT toggles between sandbox (testing) and production (live trading)
ENVIRONMENT: EnvironmentType = 'sandbox'
logger.info(f'Using environment: {ENVIRONMENT}')

environment = 'sandbox'

@dataclass
class DXLinkConfig:
    """Configuration for DXLink connection"""
    version: str = "0.1-DXF-JS/0.3.0"
    keepalive_timeout: int = 60
    accept_keepalive_timeout: int = 60
    feed_channel: int = 3  # Channel for market data feed
    auth_channel: int = 0  # Channel for auth and keepalive

@dataclass
class Quote:
    """Data structure for Quote values"""
    symbol: str
    bid_price: float
    ask_price: float
    timestamp: datetime

    @property
    def price(self) -> float:
        """Mid price between bid and ask"""
        return (self.bid_price + self.ask_price) / 2

    @classmethod
    def from_feed_data(cls, data: list) -> 'Quote':
        """Create Quote from raw feed data array"""
        try:
            # data format: ["Quote", ["Quote", "SPY", bid_price, ask_price]]
            values = data[1]  # Get the inner array
            if len(values) < 4:
                raise ValueError(f"Quote data missing required fields. Expected 4 values, got {len(values)}: {values}")
                
            return cls(
                symbol=values[1],
                bid_price=float(values[2]) if values[2] is not None else 0.0,
                ask_price=float(values[3]) if values[3] is not None else 0.0,
                timestamp=datetime.now(timezone.utc)
            )
        except Exception as e:
            raise ValueError(f"Failed to parse quote data: {e}. Data: {data}")

@dataclass
class OptionStrike:
    """Represents available strikes for an expiration"""
    strike_price: float
    call_streamer_symbol: str
    put_streamer_symbol: str

@dataclass
class OptionExpiration:
    """Represents an option expiration date with its strikes"""
    expiration_date: datetime
    days_to_expiration: int
    strikes: List[OptionStrike]

@dataclass
class GreeksData:
    """Data structure for Greeks values"""
    symbol: str
    price: float
    volatility: float
    delta: float
    gamma: float
    theta: float
    rho: float
    vega: float
    timestamp: datetime

    @classmethod
    def from_feed_data(cls, data: list) -> 'GreeksData':
        """Create GreeksData from raw feed data array"""
        values = data[1]
        return cls(
            symbol=values[1],
            price=float(values[2]),
            volatility=float(values[3]),
            delta=float(values[4]),
            gamma=float(values[5]),
            theta=float(values[6]),
            rho=float(values[7]),
            vega=float(values[8]),
            timestamp=datetime.utcnow()
        )

class GreeksManager:
    """Manages latest Greeks data for multiple symbols"""
    def __init__(self):
        self._latest_data: Dict[str, GreeksData] = {}
    
    def update(self, message: dict) -> Optional[List[GreeksData]]:
        """Update Greeks data from a message"""
        try:
            data = message.get("data", [])
            if len(data) < 2:  # Need at least event type and data array
                return None
                
            values = data[1]  # Get the data array
            if len(values) < 9:  # Need at least one complete Greeks set
                logger.error(f"Incomplete Greeks data: {values}")
                return None
                
            greeks_list = []
            i = 0
            while i < len(values):
                if values[i] == "Greeks" and i + 8 < len(values):
                    # Parse all fields with detailed logging
                    symbol = values[i + 1]
                    price = float(values[i + 2]) if values[i + 2] is not None else 0.0
                    volatility = float(values[i + 3]) if values[i + 3] is not None else 0.0
                    delta = float(values[i + 4]) if values[i + 4] is not None else 0.0
                    gamma = float(values[i + 5]) if values[i + 5] is not None else 0.0
                    theta = float(values[i + 6]) if values[i + 6] is not None else 0.0
                    rho = float(values[i + 7]) if values[i + 7] is not None else 0.0
                    vega = float(values[i + 8]) if values[i + 8] is not None else 0.0
                    
                    greeks = GreeksData(
                        symbol=symbol,
                        price=price,
                        volatility=volatility,
                        delta=delta,
                        gamma=gamma,
                        theta=theta,
                        rho=rho,
                        vega=vega,
                        timestamp=datetime.utcnow()
                    )
                    logger.debug(f"Updated Greeks for {symbol}: price=${price:.2f}")
                    self._latest_data[symbol] = greeks
                    greeks_list.append(greeks)
                    i += 9  # Skip to next potential Greeks block
                else:
                    i += 1  # Move to next element
            
            return greeks_list if greeks_list else None
            
        except Exception as e:
            logger.error(f"Error updating Greeks: {e}, message: {message}")
            return None
    
    def get_field(self, symbol: str, field: str) -> Optional[float]:
        """Get specific field value for a symbol"""
        greeks = self._latest_data.get(symbol)
        if greeks:
            return getattr(greeks, field, None)
        return None

    def get_latest(self, symbol: str) -> Optional[GreeksData]:
        """Get latest Greeks data for a symbol"""
        return self._latest_data.get(symbol)

class OptionChainManager:
    """Manages option chain data and strike selection"""
    def __init__(self, session_token: str, environment: str):
        self.session_token = session_token
        self.base_url = (settings.TASTY_SANDBOX_BASE_URL if environment == 'sandbox' 
                        else settings.TASTY_PRODUCTION_BASE_URL)
        self.headers = {"Authorization": session_token}

    async def get_atm_straddle(self, symbol: str, current_price: float) -> Tuple[Optional[str], Optional[str]]:
        """
        Get ATM straddle streamer symbols for today's expiration
        Returns: (call_streamer_symbol, put_streamer_symbol)
        """
        try:
            url = f"{self.base_url}/option-chains/{symbol}/nested"
            logger.debug(f"Requesting option chain from: {url}")
            
            response = requests.get(url, headers=self.headers)
            if response.status_code != 200:
                logger.error(f"Failed to fetch option chain: {response.status_code}")
                return None, None

            data = response.json()['data']['items'][0]
            
            # Find today's expiration
            today = datetime.now(timezone.utc).date()
            today_exp = None
            
            logger.info(f"Looking for expiration date {today}")
            for exp in data['expirations']:
                exp_date = datetime.strptime(exp['expiration-date'], "%Y-%m-%d").date()
                logger.debug(f"Checking expiration {exp_date}")
                if exp_date == today:
                    today_exp = exp
                    logger.info(f"Found today's expiration: {exp_date}")
                    break
            
            if not today_exp:
                logger.error("No same-day expiration found")
                return None, None

            # Find closest strike to current price
            strikes = today_exp['strikes']
            closest_strike = None
            min_diff = float('inf')
            
            logger.info(f"Finding closest strike to ${current_price}")
            for strike in strikes:
                strike_price = float(strike['strike-price'])
                diff = abs(strike_price - current_price)
                logger.debug(f"Strike ${strike_price}: diff = ${diff}")
                if diff < min_diff:
                    min_diff = diff
                    closest_strike = strike

            if not closest_strike:
                logger.error("No valid strike found")
                return None, None

            strike_price = float(closest_strike['strike-price'])
            logger.info(f"Selected ATM strike: ${strike_price} (diff: ${min_diff})")
            logger.info(f"Call symbol: {closest_strike['call-streamer-symbol']}")
            logger.info(f"Put symbol: {closest_strike['put-streamer-symbol']}")

            return (closest_strike['call-streamer-symbol'], 
                   closest_strike['put-streamer-symbol'])

        except Exception as e:
            logger.error(f"Error getting ATM straddle: {e}")
            return None, None

class DXLinkClient:
    """Client for streaming market data from DXLink"""
    
    def __init__(self, dxlink_url: str, quote_token: str):
        self.url = dxlink_url
        self.token = quote_token
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.config = DXLinkConfig()
        self._running = False
        self._subscribed_symbols: Dict[str, List[str]] = {}
        self._callbacks: Dict[str, Callable] = {}
        self._keepalive_task: Optional[asyncio.Task] = None
        self._authorized = False
        self.greeks_manager = GreeksManager()
        self._quote_received = asyncio.Event()
        self._latest_quote: Optional[Quote] = None

    async def connect(self) -> bool:
        """Establish connection and perform setup sequence"""
        try:
            logger.info(f"Connecting to {self.url}")
            self.websocket = await websockets.connect(self.url)
            self._running = True
            
            # Create message queues
            self._send_queue = asyncio.Queue()
            self._message_queue = asyncio.Queue()
            self._setup_queue = asyncio.Queue()
            
            # Start websocket handler task
            asyncio.create_task(self._websocket_handler())
            asyncio.create_task(self._message_processor())
            
            # Start keepalive task
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            
            # 1. Send SETUP message and wait for response
            setup_msg = {
                "type": "SETUP",
                "channel": self.config.auth_channel,
                "version": self.config.version,
                "keepaliveTimeout": self.config.keepalive_timeout,
                "acceptKeepaliveTimeout": self.config.accept_keepalive_timeout
            }
            logger.info("Sending SETUP message")
            await self._send_queue.put(setup_msg)
            
            # Wait for SETUP confirmation
            setup_response = await self._setup_queue.get()
            if setup_response.get("type") != "SETUP":
                raise Exception(f"Unexpected response to SETUP: {setup_response}")
            logger.info("SETUP completed successfully")
            
            # 2. Wait for initial AUTH_STATE (should be UNAUTHORIZED)
            auth_state = await self._setup_queue.get()
            if (auth_state.get("type") != "AUTH_STATE" or 
                auth_state.get("state") != "UNAUTHORIZED"):
                raise Exception(f"Unexpected auth state: {auth_state}")
            logger.info("Received initial UNAUTHORIZED state")
            
            # 3. Send AUTH message
            auth_msg = {
                "type": "AUTH",
                "channel": self.config.auth_channel,
                "token": self.token
            }
            logger.info("Sending AUTH message")
            await self._send_queue.put(auth_msg)
            
            # 4. Wait for AUTH_STATE (should be AUTHORIZED)
            auth_response = await self._setup_queue.get()
            if (auth_response.get("type") != "AUTH_STATE" or 
                auth_response.get("state") != "AUTHORIZED"):
                raise Exception(f"Authorization failed: {auth_response}")
            
            self._authorized = True
            logger.info("Authorization successful")
            
            # 5. Open channel for market data
            channel_msg = {
                "type": "CHANNEL_REQUEST",
                "channel": self.config.feed_channel,
                "service": "FEED",
                "parameters": {
                    "contract": "AUTO"
                }
            }
            
            logger.info("Requesting feed channel")
            await self._send_queue.put(channel_msg)
            
            # Wait for channel opened confirmation
            channel_response = await self._setup_queue.get()
            if (channel_response.get("type") != "CHANNEL_OPENED" or 
                channel_response.get("channel") != self.config.feed_channel):
                raise Exception(f"Channel opening failed: {channel_response}")
            
            logger.info("Feed channel opened successfully")
            
            # 6. Configure feed setup
            feed_setup_msg = {
                "type": "FEED_SETUP",
                "channel": self.config.feed_channel,
                "acceptAggregationPeriod": 0.1,
                "acceptDataFormat": "COMPACT",
                "acceptEventFields": {
                    "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice"],
                    "Greeks": ["eventType", "eventSymbol", "price", "delta", "gamma", "theta", "rho", "vega", "volatility"]
                }
            }
            logger.info("Setting up feed configuration")
            await self._send_queue.put(feed_setup_msg)
            
            # Wait for feed setup confirmation and verify fields
            try:
                async with asyncio.timeout(5):
                    feed_config = await self._setup_queue.get()
                    if feed_config.get("type") != "FEED_CONFIG":
                        raise Exception(f"Unexpected feed config response: {feed_config}")
                        
                    logger.info(f"Feed config response: {feed_config}")
                    logger.info("Feed setup completed successfully")
                    return True
            except asyncio.TimeoutError:
                logger.error("Feed setup timed out")
                return False
            
        except Exception as e:
            logger.error(f"Failed to connect to DXLink: {e}")
            if self.websocket:
                await self.websocket.close()
            self._running = False
            self._authorized = False
            return False
            
    async def wait_for_quote(self) -> Optional[Quote]:
        """Wait for the first quote to arrive"""
        await self._quote_received.wait()
        return self._latest_quote

    async def subscribe_quote(self, symbol: str):
        """Subscribe to Quote events for a symbol"""
        if not self._authorized:
            logger.error("Cannot subscribe: not authorized")
            return
            
        try:
            subscription_msg = {
                "type": "FEED_SUBSCRIPTION",
                "channel": self.config.feed_channel,
                "reset": False,  # Don't reset existing subscriptions
                "add": [{"type": "Quote", "symbol": symbol}]
            }
            logger.info(f"Subscribing to Quote for {symbol}")
            await self._send_queue.put(subscription_msg)
            
            if symbol not in self._subscribed_symbols:
                self._subscribed_symbols[symbol] = []
            self._subscribed_symbols[symbol].append("Quote")
            
        except Exception as e:
            logger.error(f"Failed to subscribe to Quote for {symbol}: {e}")

    async def unsubscribe_quote(self, symbol: str):
        """Unsubscribe from Quote events for a symbol"""
        if not self._authorized:
            logger.error("Cannot unsubscribe: not authorized")
            return
            
        try:
            subscription_msg = {
                "type": "FEED_SUBSCRIPTION",
                "channel": self.config.feed_channel,
                "remove": [{"type": "Quote", "symbol": symbol}]
            }
            logger.info(f"Unsubscribing from Quote for {symbol}")
            await self._send_queue.put(subscription_msg)
            
            if symbol in self._subscribed_symbols:
                self._subscribed_symbols[symbol].remove("Quote")
                if not self._subscribed_symbols[symbol]:
                    del self._subscribed_symbols[symbol]
            
        except Exception as e:
            logger.error(f"Failed to unsubscribe from Quote for {symbol}: {e}")

    async def subscribe_greeks(self, symbol: str):
        """Subscribe to Greeks events for a symbol"""
        if not self._authorized:
            logger.error("Cannot subscribe: not authorized")
            return False
            
        try:
            subscription_msg = {
                "type": "FEED_SUBSCRIPTION",
                "channel": self.config.feed_channel,
                "reset": False,  # Don't reset existing subscriptions
                "add": [{"type": "Greeks", "symbol": symbol}]
            }
            logger.info(f"Subscribing to Greeks for {symbol}")
            await self._send_queue.put(subscription_msg)
            
            if symbol not in self._subscribed_symbols:
                self._subscribed_symbols[symbol] = []
            self._subscribed_symbols[symbol].append("Greeks")
            
            # Don't wait for subscription confirmation since we're getting data anyway
            return True
            
        except Exception as e:
            logger.error(f"Failed to subscribe to Greeks for {symbol}: {e}")
            return False

    async def wait_for_greeks(self, symbol: str, timeout: float = 1.0) -> Optional[GreeksData]:
        """Wait for Greeks data for a specific symbol"""
        try:
            start_time = datetime.now().timestamp()
            while datetime.now().timestamp() - start_time < timeout:
                greeks = self.greeks_manager.get_latest(symbol)
                if greeks:
                    logger.info(f"Received Greeks for {symbol}: price=${greeks.price:.2f}")
                    return greeks
                await asyncio.sleep(0.1)
            logger.error(f"Timeout waiting for Greeks data for {symbol}")
            return None
        except Exception as e:
            logger.error(f"Error waiting for Greeks data: {e}")
            return None

    async def _send_message(self, message: dict) -> None:
        """Send a message to the websocket"""
        await self._send_queue.put(message)
            
    async def _websocket_handler(self):
        """Handle websocket communication"""
        while self._running and self.websocket:
            try:
                # Check for outgoing messages
                try:
                    message = self._send_queue.get_nowait()
                    await self.websocket.send(json.dumps(message))
                except asyncio.QueueEmpty:
                    pass
                    
                # Check for incoming messages
                try:
                    async with asyncio.timeout(0.1):  # Small timeout to allow checking send queue
                        raw_message = await self.websocket.recv()
                        message = json.loads(raw_message)
                        await self._message_queue.put(message)
                except asyncio.TimeoutError:
                    continue
                    
            except Exception as e:
                logger.error(f"Error in websocket handler: {e}")
                await asyncio.sleep(1)

    async def _message_processor(self):
        """Process messages from the queue"""
        while self._running:
            try:
                message = await self._message_queue.get()
                logger.debug(f"Processing message: {message}")
                
                # During setup, route setup messages to the setup queue
                if hasattr(self, '_setup_queue'):
                    msg_type = message.get("type", "")
                    if msg_type in ["SETUP", "AUTH_STATE", "CHANNEL_OPENED", "FEED_CONFIG"]:
                        await self._setup_queue.put(message)
                        continue
                
                if message.get("type") == "FEED_DATA":
                    data = message.get("data", [])
                    if data:
                        event_type = data[0]
                        if event_type == "Greeks":
                            try:
                                logger.debug(f"Processing Greeks data: {data}")
                                greeks_list = self.greeks_manager.update(message)
                                if greeks_list and "greeks" in self._callbacks:
                                    for greeks in greeks_list:
                                        await self._callbacks["greeks"](greeks)
                                else:
                                    logger.warning(f"Greeks update failed or no callback registered for {data}")
                            except Exception as e:
                                logger.error(f"Error processing Greeks data: {e}, data: {data}")
                        elif event_type == "Quote":
                            try:
                                quote = Quote.from_feed_data(data)
                                self._latest_quote = quote
                                self._quote_received.set()
                                if "quote" in self._callbacks:
                                    await self._callbacks["quote"](quote)
                            except Exception as e:
                                logger.error(f"Error processing quote data: {e}, data: {data}")
                elif message.get("type") == "AUTH_STATE":
                    await self._handle_auth_state(message)
                elif message.get("type") == "FEED_SUBSCRIPTION":
                    logger.info(f"Subscription status update: {message}")
                elif message.get("type") == "KEEPALIVE":
                    # Send keepalive response
                    await self._send_queue.put({
                        "type": "KEEPALIVE",
                        "channel": message.get("channel", 0)
                    })
                    
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                await asyncio.sleep(1)

    async def _handle_auth_state(self, message: Dict[str, Any]):
        """Handle authentication state changes"""
        state = message.get("state")
        logger.info(f"Auth state changed to: {state}")

    async def _receive_message(self) -> Dict[str, Any]:
        """Receive and parse a message from the WebSocket"""
        if self.websocket:
            message = await self.websocket.recv()
            return json.loads(message)
        return {}

    def on_greeks(self, callback: Callable[[GreeksData], None]):
        """Register callback for Greeks events"""
        self._callbacks["greeks"] = callback

    def on_quote(self, callback: Callable[[Quote], None]):
        """Register callback for Quote events"""
        self._callbacks["quote"] = callback

    async def close(self):
        """Close the WebSocket connection"""
        self._running = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self.websocket:
            await self.websocket.close()

    async def _keepalive_loop(self):
        """Send keepalive messages every 30 seconds"""
        try:
            while self._running:
                await asyncio.sleep(30)  # Send keepalive every 30 seconds (half the timeout)
                if self._running and self._authorized:
                    await self._send_queue.put({
                        "type": "KEEPALIVE",
                        "channel": self.config.auth_channel
                    })
        except Exception as e:
            logger.error(f"Error in keepalive loop: {e}")

@dataclass
class OptionLeg:
    """Represents a single option leg in a trade"""
    symbol: str  # OCC-style symbol
    quantity: int  # Positive for long, negative for short
    entry_price: float
    streamer_symbol: Optional[str] = None  # DXFeed symbol
    current_price: Optional[float] = None

    def get_pnl(self) -> Optional[float]:
        """Calculate PnL for this leg. Each contract represents 100 shares."""
        if self.current_price is None:
            return None
        return (self.current_price - self.entry_price) * self.quantity * 100

class PnLTracker:
    """Tracks PnL for option trades"""
    def __init__(self, legs: List[OptionLeg]):
        self.legs = legs
        
    def update_prices(self, greeks_data: GreeksData):
        """Update current price for the leg matching this Greeks data"""
        for leg in self.legs:
            if leg.streamer_symbol == greeks_data.symbol:
                leg.current_price = greeks_data.price
                break
        
    def get_current_pnl(self) -> Optional[float]:
        """Calculate total PnL across all legs"""
        leg_pnls = [leg.get_pnl() for leg in self.legs]
        if None in leg_pnls:  # If any leg is missing a current price
            return None
        total_pnl = sum(leg_pnls)
        logger.debug(f"Leg PnLs: {leg_pnls}, Total: ${total_pnl:.2f}")
        return total_pnl

async def setup_spy_straddle_tracking(environment: str, session_token: str) -> Optional[PnLTracker]:
    """Set up PnL tracking for SPY 0-DTE ATM straddle"""
    try:
        # Get quote token and URL
        quote_token, dxlink_url = get_quote_token(environment, session_token)
        if not quote_token or not dxlink_url:
            logger.error("Failed to get quote token")
            return None

        # Create option chain manager
        chain_manager = OptionChainManager(session_token, environment)
        
        # Create DXLink client and connect
        client = DXLinkClient(dxlink_url, quote_token)
        if not await client.connect():
            logger.error("Failed to connect to DXLink")
            return None

        # Subscribe to SPY quotes
        await client.subscribe_quote("SPY")
        
        # Wait for first quote
        quote = await client.wait_for_quote()
        if not quote:
            logger.error("Failed to get SPY quote")
            return None

        # Unsubscribe from quotes since we have what we need
        await client.unsubscribe_quote("SPY")
        logger.info(f"Got SPY quote: ${quote.price:.2f}")
        
        # Get ATM straddle streamer symbols
        call_symbol, put_symbol = await chain_manager.get_atm_straddle("SPY", quote.price)
        if not call_symbol or not put_symbol:
            logger.error("Failed to get straddle streamer symbols")
            return None

        # Create PnL tracker first so callbacks are ready
        legs = [
            OptionLeg(
                symbol=call_symbol,
                quantity=1,
                entry_price=0,  # Will be updated with real price
                streamer_symbol=call_symbol,
                current_price=0
            ),
            OptionLeg(
                symbol=put_symbol,
                quantity=1,
                entry_price=0,  # Will be updated with real price
                streamer_symbol=put_symbol,
                current_price=0
            )
        ]
        
        pnl_tracker = PnLTracker(legs)
        
        # Set up callback to update PnL
        async def handle_greeks(greeks_data: GreeksData):
            pnl_tracker.update_prices(greeks_data)
            current_pnl = pnl_tracker.get_current_pnl()
            if current_pnl is not None:
                print(f"Current Straddle PnL: ${current_pnl:.2f}")
                
        client.on_greeks(handle_greeks)

        # Subscribe to Greeks for both options
        logger.info("Subscribing to Greeks for both options...")
        await client.subscribe_greeks(call_symbol)
        await client.subscribe_greeks(put_symbol)
        
        # Wait for initial Greeks data (should arrive immediately after subscription)
        initial_call_greeks = await client.wait_for_greeks(call_symbol, timeout=2.0)
        initial_put_greeks = await client.wait_for_greeks(put_symbol, timeout=2.0)
        
        if not initial_call_greeks or not initial_put_greeks:
            logger.error("Failed to get initial option prices")
            return None
            
        logger.info(f"Initial call price: ${initial_call_greeks.price:.2f}")
        logger.info(f"Initial put price: ${initial_put_greeks.price:.2f}")
        
        # Update legs with actual entry prices
        for leg in legs:
            if leg.streamer_symbol == call_symbol:
                leg.entry_price = initial_call_greeks.price
                leg.current_price = initial_call_greeks.price
            elif leg.streamer_symbol == put_symbol:
                leg.entry_price = initial_put_greeks.price
                leg.current_price = initial_put_greeks.price

        total_cost = sum(leg.entry_price * 100 for leg in legs)
        logger.info(f"Total straddle cost: ${total_cost:.2f}")

        return pnl_tracker
            
    except Exception as e:
        logger.error(f"Error setting up straddle tracking: {e}")
        return None

async def setup_pnl_tracking(environment: str, session_token: str, legs: List[OptionLeg]) -> Optional[PnLTracker]:
    """Set up PnL tracking for a multi-leg option position"""
    try:
        # Get quote token and URL
        quote_token, dxlink_url = get_quote_token(environment, session_token)
        if not quote_token or not dxlink_url:
            logger.error("Failed to get quote token")
            return None
            
        # Get streamer symbols for all legs
        for leg in legs:
            streamer_symbol = get_streamer_symbol(environment, session_token, leg.symbol)
            if not streamer_symbol:
                logger.error(f"Failed to get streamer symbol for {leg.symbol}")
                return None
            leg.streamer_symbol = streamer_symbol
            
        # Create PnL tracker
        pnl_tracker = PnLTracker(legs)
        
        # Create DXLink client
        client = DXLinkClient(dxlink_url, quote_token)
        if not await client.connect():
            logger.error("Failed to connect to DXLink")
            return None
            
        # Subscribe to Greeks for all legs
        for leg in legs:
            await client.subscribe_greeks(leg.streamer_symbol)
            
        # Set up callback to update PnL
        async def handle_greeks(greeks_data: GreeksData):
            pnl_tracker.update_prices(greeks_data)
            current_pnl = pnl_tracker.get_current_pnl()
            if current_pnl is not None:
                logger.info(f"Current PnL: ${current_pnl:.2f}")
                
        client.on_greeks(handle_greeks)
        
        return pnl_tracker
        
    except Exception as e:
        logger.error(f"Error setting up PnL tracking: {e}")
        return None

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
    response = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
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

def get_quote_token(environment: EnvironmentType, session_token: str):
    """
    Get an API quote token for streaming market data through DXLink.
    
    This token identifies the customer to TastyTrade's quote provider (DXLink).
    Note: You must be a registered tastytrade customer (with an opened account) to access quote streaming.

    Args:
        environment (str): The environment type ('sandbox' or 'production').
        session_token (str): Valid session token for authentication.

    Returns:
        tuple[str, str]: A tuple of (quote_token, dxlink_url) if successful, (None, None) if failed.

    Examples:
        quote_token, dxlink_url = get_quote_token('sandbox', session_token)
    """
    with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
        quote_token = db.get('quote_token')
        dxlink_url = db.get('dxlink_url')
        quote_token_expiry = db.get('quote_token_expiry')

        # Check if we have a valid token that hasn't expired
        if quote_token and dxlink_url and quote_token_expiry and datetime.now() < quote_token_expiry:
            logger.success('Found existing quote token.', extra={'quote_token': quote_token})
            logger.info(f'Existing quote token will expire at {quote_token_expiry}.')
            return quote_token, dxlink_url

    # If we get here, we either don't have a token or it's expired
    logger.warning('Quote token expired or invalid, requesting new quote token...')
    if environment == 'sandbox':
        url = f"{settings.TASTY_SANDBOX_BASE_URL}/api-quote-tokens"
        logger.info(f'Using environment:{environment} with base url: {url}')
    else:
        url = f"{settings.TASTY_PRODUCTION_BASE_URL}/api-quote-tokens"
        logger.info(f'Using environment:{environment} with base url: {url}')

    headers = {
        "Authorization": session_token
    }
    
    logger.debug('Generated headers with session token.')
    response = requests.get(url, headers=headers)  # Using GET instead of POST
    logger.info(f'GET request: {response}')

    if response.status_code == 200:  # Success code for GET is 200, not 201
        logger.success(f'Response status code: {response.status_code}. Received quote token.')
        data = response.json()['data']
        new_quote_token = data['token']
        new_dxlink_url = data['dxlink-url']
        # Quote tokens are valid for 24 hours per documentation
        new_token_expiry = datetime.now() + timedelta(hours=24)
        logger.debug(f'Saved new quote token expiring at: {new_token_expiry}.')

        # Open a new shelf connection to store the token and dxlink url
        with shelve.open(str(Path(settings.SESSION_SHELF_DIR) / 'session_data')) as db:
            db['quote_token'] = new_quote_token
            db['dxlink_url'] = new_dxlink_url
            db['quote_token_expiry'] = new_token_expiry
            logger.success('Stored new quote token, dxlink url, and token expiry.')

        return new_quote_token, new_dxlink_url
    else:
        if response.status_code == 404:
            error_data = response.json().get('error', {})
            if error_data.get('code') == 'quote_streamer.customer_not_found_error':
                logger.error('Quote token request failed: You must be a registered tastytrade customer with an opened account to access quote streaming.')
        logger.error(f'Quote token request failed with response code: {response.status_code}.')
        logger.debug(f'{response.text}')
        return None, None

def get_streamer_symbol(environment: str, session_token: str, occ_symbol: str) -> Optional[str]:
    """Convert OCC-style option symbol to streamer symbol"""
    try:
        base_url = (settings.TASTY_SANDBOX_BASE_URL if environment == 'sandbox' 
                   else settings.TASTY_PRODUCTION_BASE_URL)
        
        headers = {"Authorization": session_token}
        
        # Extract information from OCC symbol
        # Format: Symbol + YY + MM + DD + C/P + Strike
        # Example: SPY240119C500
        
        underlying = occ_symbol[:3]  # This is simplified, should handle variable length symbols
        url = f"{base_url}/option-chains/{underlying}"
        
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            logger.error(f"Failed to fetch option chain: {response.status_code}")
            return None
            
        data = response.json()['data']
        
        # Find matching option in the chain
        for item in data['items']:
            if item.get('occ-symbol') == occ_symbol:
                return item.get('streamer-symbol')
                
        logger.error(f"No matching option found for {occ_symbol}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting streamer symbol: {e}")
        return None

# Example usage
async def main():
    environment = 'sandbox'
    session_token = get_session_token(environment)
    if session_token:
        pnl_tracker = await setup_spy_straddle_tracking(environment, session_token)
        if pnl_tracker:
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                logger.info("Stopping PnL tracking")

if __name__ == "__main__":
    asyncio.run(main())
