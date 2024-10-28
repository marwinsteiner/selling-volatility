# Systematic Short Vol - Extremely Short TF - 0DTE SPX Credit Spread - Strategy Handbook

## Table of Contents

1. Strategy Overview
2. Market Analysis Framework
3. Trade Construction
4. Risk Management
5. Technical Implementation
6. Performance Metrics

## 1. Strategy Overview

### Core Concept

- Systematically sells 0-DTE (same-day expiration) credit spreads on SPX options.
- Direction determined by trend-following regime
- Entry calculation at 09:35 ET each day to allow for day trend formation
- Strikes selected based on VIX-implied expected move in the SPX

### Key Components

- Trend regime detection using moving averages
- Volatility analysis using VIX
- Automated order execution via TastyTrade
- Real-time position monitoring

## 2. Market Analysis Framework

### Trend Regime Detection

- 20 day moving average on SPY (1 month trend)
- 60 day moving average on SPY (3 month trend)
- Regime = 1 if price > 20 day moving average, else 0

### Volatility Analysis

- Calculate the VIX-implied expected move in the SPX
- Use VIX as the volatility input
- $$E_m = \frac{p_{VIX}}{\sqrt{252}} \times 0.5$$ where $$E_m$$ is the expected move and $$p_{VIX}$$ is the level of the
  VIX.

## 3. Trade Construction
### Direction Selection
- Trend regime = 0: Sell call credit spread
- Trend regime = 1: Sell put credit spread

### Strike Selection
```python
# For Call Credit Spreads:
upper_price = price + (price * expected_move)
short_strike = min strike above upper_price
long_strike = next strike above short_strike

# For Put Credit Spreads:
lower_price = price - (price * expected_move)
short_strike = max strike below lower_price
long_strike = next strike below short_strike
```

### Order Execution
- Uses mid-price for initial pricing
- Implements optimal pricing algorithm: `optimal_price = round(((mid_price - .05) / .05) * .05, 2)`

## 4. Risk Management

### Position Sizing
- Fixed 1-contract position size, though this can be scaled up depending on portfolio risk limits
- $3000 initial capital requirement
- Defined risk trade - from first principles

### Risk Parameters
- Maximum loss limited to spread width minus credit received
- Natural stop at max loss (no manual intervention required)
- expected move buffer provides some probability cushion.

### Trade Management
- Hold until expiry
- No active management required - set - forget - account.
- Profit taking at full premium capture even when not expired.

## 5. Technical Implementation

### Data Infrastructure
!Temporarily!
- Polygon.io: market data
- TastyTrade: execution, options chains
- NYSE calendar: trading dates

### Key Functions
1. Regime Detection
2. Strike Selection
3. Order Construction
4. Execution Logic
5. Position Monitoring

### Error Handling
- Continuous API connection monitoring
- Order validation before submission
