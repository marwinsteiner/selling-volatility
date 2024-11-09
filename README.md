# A Working System for Selling 0-DTE SPX Options

This script contains the functionality needed to backtest and deploy a systematic 0-DTE option selling strategy on SPX.

We sell 0-DTE SPX credit spreads, using half the VIX1D expected move for strike selection and a moving average to select
the direction (whether puts or calls). This is a typical negatively skewed strategy.

<img src = "https://github.com/quantgalore/selling-volatility/blob/main/historical-premium.png">

We go by premium rather than delta because the delta on the 0-days are unreliable because they change so much during the
life of the option.

The latest update adds scheduling, meaning you can now run this script as an always-on script on a virtual machine,
cloud computer, or RaspberryPi. The downside of a physical hosting option is you have the incidence of the risk of a
power cut, a fire, water damage, an internet outage, or quite simply, something breaking. If this leads to missing a
significant enough number of the 252 annual occurrences, it can be worth looking at running it in the cloud and paying
some hosting fees.

`*\docs` contains some additional, more in-depth documentation, `*\session_shelf` is a directory created once you
run `*\src\production-tastytrade.py` and contains your tastytrade session token and its expiry. `*\src\backtest\`
contains a script which you can run if you're not yet convinced this has edge. `*\src\logs\` contains log files of
console output -- another generated directory if it doesn't already exist. `*\src` contains our
scripts. `*\src\production-tastytrade.py` is the script you want to run if you can have the script running
permanently -- locally or in the cloud. `*\src\spread-production.py` is a cli-based output of that day's trade and will
generate errors until trading time of 09:36 ET is reached. It contains the same functionality
as `*\src\production-tastytrade.py` except it will not send your order through to tastytrade, it only outputs the legs
and live PnL tracking to the console as a common-sense check if you need it.

`*\account-manager.py` is still a work in progress but aims to track PnL, BPR, and other stats like this using the tasty
API, for this position specifically. It writes this information to a trade-related MongoDB, for our later reference.

We use dynaconf for configuration so you'll have to set this up accordingly and copy in your secrets into the
appropriate file.

Finally, none of this should be construed as financial advice and the author(s) of this library will not be made
responsible for any losses incurred -- any losses incurred are strictly your own. This repo is for educational
purposes only.