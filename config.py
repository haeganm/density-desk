"""Density Desk configuration. All money values in dollars unless noted."""
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "desk.db"
KILL_FILE = ROOT / "KILL"                      # presence halts all order placement
ARMED_FILE = ROOT / "state" / "ARMED"          # ABSENCE disarms trading (dashboard toggle)
SECRETS = ROOT / "secrets"
KALSHI_PEM = SECRETS / "kalshi.pem"
KALSHI_KEY_ID_FILE = SECRETS / "kalshi_key_id.txt"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DERIBIT_BASE = "https://www.deribit.com/api/v2"
MODEL_VERSION = "r4"

# Series to trade (see docs/market-structure.md)
SERIES = {
    "BTC": {"ladder": "KXBTCD", "range": "KXBTC", "deribit_ccy": "BTC", "deribit_index": "btc_usd"},
    "ETH": {"ladder": "KXETHD", "range": "KXETH", "deribit_ccy": "ETH", "deribit_index": "eth_usd"},
}

# --- Fees (verified against live fills; see market-structure.md) ---
TAKER_FEE_MULT = 0.07            # fee = mult * p * (1-p) * count, rounded up to $0.0001
MAKER_FEE_FRACTION = 0.0         # verified $0.00 on live maker fills 2026-07-15

# --- Edge thresholds (dollars per contract, after fees) ---
MAKER_EDGE_MIN = 0.02
TAKER_EDGE_MIN = 0.03
MAKER_ENABLED = True             # post-only resting quotes in the maker band
MAKER_MAX_RESTING = 3            # resting quotes at once (one per ticker+side)
# humility rails, tuned by live fills through 2026-07-15: sub-15c entries realized
# 4-13% on 21-27% predicted and lost every cohort; the 15c+ book was profitable
MIN_BUY_PRICE = 0.15             # never buy sub-15c contracts
MAX_BUY_PRICE = 0.85             # never buy above 85c: the upside there is <=15c, so a
                                 # 3c "edge" claims the market is wrong by >=20% of the
                                 # contract's whole remaining value — implausible when our
                                 # measured edge over that market is +3.0% Brier, 95% CI
                                 # [-0.0012,+0.0101] i.e. indistinguishable from zero
                                 # (n=332 tradable contracts, 2026-07-15). Live cost of
                                 # ignoring this: -$1.64 on 4 contracts at 0.87/0.95.
MAX_FAIR_RATIO = 2.0             # skip when fair > ratio x price ("too good" filter)
PER_EVENT_CAP = 5.00             # max cost across all strikes/sides of one settle event
                                 # (~20% of a $25 book; cuts correlated-cluster variance)
REQUOTE_TRIGGER = 0.01           # cancel/replace resting order when fair moves this much

# --- Sizing & rails (enforced in engine/rails.py, never overridable by the operator LLM) ---
# Kelly bankroll is the live cash balance each cycle. These remain hard safety caps.
KELLY_FRACTION = 0.25
PER_TRADE_CAP = 2.00             # max cost basis per order
SHAKEOUT_CAP = 2.00              # forced cap until shakeout complete
SHAKEOUT = False                 # live fills verified (fees, settlement, journal)
DAILY_LOSS_LIMIT = 5.00          # realized; breach -> halt until next UTC day
STRATEGY_EPOCH = int(dt.datetime(2026, 7, 14, 14, 30, tzinfo=dt.UTC).timestamp())
                                 # Round-3 model live at 2026-07-14 14:30Z.
                                 # The loss ledger follows the strategy VERSION —
                                 # the retired model's book doesn't bill this one.
MAX_OPEN_EXPOSURE = 12.00        # total cost basis of open positions + resting orders
ORDERS_PER_CYCLE = 2             # pacing: max new orders per scan cycle
STALE_DATA_HALT_S = 300          # halt if Deribit or Kalshi data older than this

# --- Engine cadence ---
REFRESH_S = 120                  # density + scan cycle
REFRESH_FINAL_HOUR_S = 30        # cycle time when nearest settle < FINAL_WINDOW_S away
FINAL_WINDOW_S = 2 * 3600
SETTLEMENT_AVG_WINDOW_S = 60     # settle = avg of final 60s -> total var sigma^2*(T - 2W/3)
BASIS_ALARM_BPS = 10             # model median vs market-implied center: drop event's edges
MODEL_MARKET_IQR_MIN = 2 / 3
MODEL_MARKET_IQR_MAX = 1.50
MIN_EVENT_PROFIT_PROB = 0.55     # required under both model and market distributions
# Our fair value is an estimate, not truth: shrink it toward the market mid before
# calling anything an edge. Measured fill-level adverse selection (2026-07-15:
# promised +$3.98 -> realized -$3.80, drift -0.195) says the market's price carries
# real information exactly where we disagree. Only the SURVIVING disagreement trades.
# 1.0 = trust the model completely (the behaviour that lost money); raise toward 1.0
# only when out-of-sample fills prove the model beats the mid.
MODEL_TRUST = 0.70

DASHBOARD_PORT = 8777
