### Core System Analysis - SPX Options Trading Platform
Priority Level: Current Implementation

Input/Output Contracts:
- Inputs:
  * Market Data:
    - SPX index prices (via Polygon.io API)
    - VIX index data for volatility regime detection
    - Options chain data (from TastyTrade and Polygon.io)
    - Real-time quotes for options
  * Configuration:
    - Environment settings (sandbox/production)
    - API credentials (TastyTrade, Polygon.io)
    - Trading parameters (timing, thresholds)
    - Email notification settings
  
- Outputs:
  * Trading Actions:
    - Generated credit spread orders
    - Position tracking data
    - PnL calculations
  * Monitoring Data:
    - Real-time position status
    - Account balance updates
    - Execution logs
    - Email reports

Dependencies:
1. External Services:
   - TastyTrade API (order execution, account management)
   - Polygon.io API (market data)
   - MongoDB (data storage)
   - SMTP server (email notifications)

2. Python Libraries:
   - pandas (data manipulation)
   - numpy (calculations)
   - websockets (real-time connections)
   - requests (API calls)
   - schedule (trade timing)
   - loguru (logging)
   - dynaconf (configuration)

Integration Points:
1. Market Data Integration:
   - Polygon.io WebSocket/REST APIs
   - Real-time data streaming
   - Historical data retrieval

2. Trading Platform Integration:
   - TastyTrade order execution
   - Account management
   - Position tracking
   - WebSocket connections

3. Database Integration:
   - MongoDB for position storage
   - Trade history tracking
   - Balance updates

4. Notification System:
   - Email alerts
   - Logging system
   - Execution reports

Success Criteria:
1. Functional Requirements:
   - Accurate regime detection (trend/volatility)
   - Proper strike price selection
   - Successful order execution
   - Real-time position tracking
   - Accurate PnL calculation

2. Performance Requirements:
   - Sub-second market data updates
   - Order execution within 1 second
   - 99.9% uptime during market hours
   - Reliable WebSocket connections

3. Risk Management:
   - Position size limits
   - Strike price validation
   - Account balance checks
   - Environment-based safety checks

Validation Steps:
1. System Health:
   - WebSocket connection stability
   - Database connectivity
   - API response times
   - Memory usage monitoring

2. Trading Logic:
   - Regime detection accuracy
   - Strike price calculation verification
   - Order generation validation
   - PnL calculation accuracy

3. Risk Controls:
   - Position limit enforcement
   - Balance requirement checks
   - Environmental safety guards
   - Error handling verification

4. Integration Testing:
   - End-to-end order flow
   - Market data processing
   - Position tracking accuracy
   - Notification system reliability

Interconnections:
1. Data Flow:
   - Market Data → Regime Detection → Strategy Selection
   - Strategy Selection → Order Generation → Execution
   - Execution → Position Tracking → PnL Calculation
   - System Events → Logging → Notification

2. Component Dependencies:
   - account-manager.py: Core WebSocket and position tracking
   - production-tastytrade.py: Main trading execution
   - spread-production.py: Real-time monitoring
   - spread-backtest-settlement.py: Strategy validation

3. Service Integration:
   - TastyTrade ↔ Order Execution
   - Polygon.io ↔ Market Data
   - MongoDB ↔ Data Storage
   - SMTP ↔ Notifications