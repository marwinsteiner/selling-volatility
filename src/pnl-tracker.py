import websockets
import json
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass

@dataclass
class DXLinkConfig:
    """Configuration for DXLink connection"""
    version: str = "0.1-DXF-JS/0.3.0"
    keepalive_timeout: int = 60
    accept_keepalive_timeout: int = 60
    feed_channel: int = 3  # Channel for market data feed
    auth_channel: int = 0  # Channel for auth and keepalive

@dataclass
class OptionLeg:
    """Represents a single option leg in a trade"""
    symbol: str  # OCC-style symbol
    quantity: int  # Positive for long, negative for short
    entry_price: float
    streamer_symbol: Optional[str] = None  # DXFeed symbol

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
    
    def update(self, message: dict) -> Optional[GreeksData]:
        """Update Greeks data from a feed message"""
        try:
            data = message.get('data', [])
            if not data or data[0] != 'Greeks':
                return None
            
            greeks = GreeksData.from_feed_data(data)
            self._latest_data[greeks.symbol] = greeks
            return greeks
        except Exception as e:
            logger.error(f"Error updating Greeks data: {e}")
            return None
    
    def get_field(self, symbol: str, field: str) -> Optional[float]:
        """Get specific field value for a symbol"""
        greeks = self._latest_data.get(symbol)
        if greeks:
            return getattr(greeks, field, None)
        return None

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

    async def connect(self):
        """Establish connection and perform setup sequence"""
        try:
            logger.info(f"Connecting to {self.url}")
            self.websocket = await websockets.connect(self.url)
            self._running = True
            
            # 1. Send SETUP message and wait for response
            setup_msg = {
                "type": "SETUP",
                "channel": self.config.auth_channel,
                "version": self.config.version,
                "keepaliveTimeout": self.config.keepalive_timeout,
                "acceptKeepaliveTimeout": self.config.accept_keepalive_timeout
            }
            logger.info("Sending SETUP message")
            await self._send_message(setup_msg)
            
            setup_response = await self._receive_message()
            if setup_response.get("type") != "SETUP":
                raise Exception(f"Unexpected response to SETUP: {setup_response}")
            logger.info("SETUP completed successfully")
            
            # 2. Wait for AUTH_STATE and send authorization
            auth_state = await self._receive_message()
            if (auth_state.get("type") != "AUTH_STATE" or 
                auth_state.get("state") != "UNAUTHORIZED"):
                raise Exception(f"Unexpected auth state: {auth_state}")
            
            auth_msg = {
                "type": "AUTH",
                "channel": self.config.auth_channel,
                "token": self.token
            }
            logger.info("Sending AUTH message")
            await self._send_message(auth_msg)
            
            # Wait for authorization confirmation
            auth_response = await self._receive_message()
            if (auth_response.get("type") != "AUTH_STATE" or 
                auth_response.get("state") != "AUTHORIZED"):
                raise Exception(f"Authorization failed: {auth_response}")
            
            self._authorized = True
            logger.info("Authorization successful")
            
            # 3. Open channel for market data
            channel_msg = {
                "type": "CHANNEL_REQUEST",
                "channel": self.config.feed_channel,
                "service": "FEED",
                "parameters": {"contract": "AUTO"}
            }
            logger.info("Requesting channel")
            await self._send_message(channel_msg)
            
            # Wait for channel confirmation
            channel_response = await self._receive_message()
            if (channel_response.get("type") != "CHANNEL_OPENED" or 
                channel_response.get("channel") != self.config.feed_channel):
                raise Exception(f"Channel opening failed: {channel_response}")
            
            logger.info("Channel opened successfully")
            
            # 4. Configure feed setup for Greeks data
            feed_setup_msg = {
                "type": "FEED_SETUP",
                "channel": self.config.feed_channel,
                "acceptAggregationPeriod": 0.1,
                "acceptDataFormat": "COMPACT",
                "acceptEventFields": {
                    "Greeks": [
                        "eventType",
                        "eventSymbol",
                        "price",
                        "volatility",
                        "delta",
                        "gamma",
                        "theta",
                        "rho",
                        "vega"
                    ]
                }
            }
            logger.info("Setting up feed")
            await self._send_message(feed_setup_msg)
            
            # Wait for feed configuration confirmation
            feed_config = await self._receive_message()
            if feed_config.get("type") != "FEED_CONFIG":
                raise Exception(f"Feed setup failed: {feed_config}")
            
            logger.info("Feed setup completed successfully")
            
            # Start keepalive task
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            
            # Start message handling loop
            asyncio.create_task(self._message_handler())
            
            logger.success("Successfully connected to DXLink and completed setup sequence")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to DXLink: {e}")
            if self.websocket:
                await self.websocket.close()
            self._running = False
            self._authorized = False
            return False

    async def subscribe_greeks(self, symbol: str):
        """Subscribe to Greeks events for a symbol"""
        if not self._authorized:
            logger.error("Cannot subscribe: not authorized")
            return
            
        try:
            subscription_msg = {
                "type": "FEED_SUBSCRIPTION",
                "channel": self.config.feed_channel,
                "reset": True,  # Reset to ensure clean subscription
                "add": [{"type": "Greeks", "symbol": symbol}]
            }
            logger.info(f"Subscribing to Greeks for {symbol}")
            await self._send_message(subscription_msg)
            self._subscribed_symbols[symbol] = ["Greeks"]
            
        except Exception as e:
            logger.error(f"Failed to subscribe to Greeks for {symbol}: {e}")

    async def _keepalive_loop(self):
        """Send keepalive messages every 30 seconds"""
        while self._running and self._authorized:
            try:
                keepalive_msg = {
                    "type": "KEEPALIVE",
                    "channel": self.config.auth_channel
                }
                await self._send_message(keepalive_msg)
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Keepalive error: {e}")
                await asyncio.sleep(1)

    async def _message_handler(self):
        """Handle incoming messages"""
        while self._running and self.websocket:
            try:
                raw_message = await self.websocket.recv()
                print(f"Raw message received: {raw_message}")
                
                message = json.loads(raw_message)
                if message.get("type") == "FEED_DATA":
                    greeks = self.greeks_manager.update(message)
                    if greeks and "greeks" in self._callbacks:
                        await self._callbacks["greeks"](greeks)
                elif message.get("type") == "AUTH_STATE":
                    await self._handle_auth_state(message)
                    
            except Exception as e:
                logger.error(f"Error handling message: {e}")
                await asyncio.sleep(1)

    async def _handle_auth_state(self, message: Dict[str, Any]):
        """Handle authentication state changes"""
        state = message.get("state")
        logger.info(f"Auth state changed to: {state}")

    async def _send_message(self, message: Dict[str, Any]):
        """Send a message to the WebSocket"""
        if self.websocket:
            await self.websocket.send(json.dumps(message))

    async def _receive_message(self) -> Dict[str, Any]:
        """Receive and parse a message from the WebSocket"""
        if self.websocket:
            message = await self.websocket.recv()
            return json.loads(message)
        return {}

    def on_greeks(self, callback: Callable[[GreeksData], None]):
        """Register callback for Greeks events"""
        self._callbacks["greeks"] = callback

    async def close(self):
        """Close the WebSocket connection"""
        self._running = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self.websocket:
            await self.websocket.close()

class PnLTracker:
    """Tracks PnL for option trades"""
    def __init__(self, legs: List[OptionLeg]):
        self.legs = legs
        self.initial_value = self._calculate_initial_value()
        self._latest_values: Dict[str, float] = {}
        
    def _calculate_initial_value(self) -> float:
        """Calculate the initial value of the position"""
        return sum(leg.quantity * leg.entry_price for leg in self.legs)
    
    def update_prices(self, greeks_data: GreeksData):
        """Update prices based on new Greeks data"""
        self._latest_values[greeks_data.symbol] = greeks_data.price
        
    def get_current_pnl(self) -> Optional[float]:
        """Calculate current PnL if all prices are available"""
        if not all(leg.streamer_symbol in self._latest_values for leg in self.legs):
            return None
            
        current_value = sum(
            leg.quantity * self._latest_values[leg.streamer_symbol]
            for leg in self.legs
        )
        return current_value - self.initial_value

def get_session_token(environment: str) -> Optional[str]:
    """Get session token for TastyTrade API"""
    try:
        base_url = (settings.TASTY_SANDBOX_BASE_URL if environment == 'sandbox' 
                   else settings.TASTY_PRODUCTION_BASE_URL)
        
        # Your session token logic here
        # This should be implemented based on your authentication requirements
        pass
        
    except Exception as e:
        logger.error(f"Error getting session token: {e}")
        return None

def get_quote_token(environment: str, session_token: str) -> Tuple[Optional[str], Optional[str]]:
    """Get quote token and DXLink URL for streaming"""
    try:
        base_url = (settings.TASTY_SANDBOX_BASE_URL if environment == 'sandbox' 
                   else settings.TASTY_PRODUCTION_BASE_URL)
        
        headers = {"Authorization": session_token}
        
        # Your quote token logic here
        # This should be implemented based on your streaming requirements
        pass
        
    except Exception as e:
        logger.error(f"Error getting quote token: {e}")
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
                print(f"Current PnL: ${current_pnl:.2f}")
                
        client.on_greeks(handle_greeks)
        
        return pnl_tracker
        
    except Exception as e:
        logger.error(f"Error setting up PnL tracking: {e}")
        return None
