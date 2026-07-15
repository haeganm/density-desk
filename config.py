"""Weather Desk configuration. All money values in dollars unless noted."""
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
MODEL_VERSION = "w1"             # weather desk, generation 1

# Cities to trade. One, done properly: Central Park (KNYC) is the station named
# in the KXHIGHNY settlement rule. Each city needs its own fitted bias -- do not
# add one without running scripts/fit_weather.py for it.
CITIES = {
    "NYC": {"series": "KXHIGHNY", "lat": 40.7794, "lon": -73.9692,
            "tz": "America/New_York", "iem_network": "NY_ASOS", "iem_station": "NYC"},
}

# --- Fees (verified against live fills; see market-structure.md) ---
TAKER_FEE_MULT = 0.07            # fee = mult * p * (1-p) * count, rounded up to $0.0001
MAKER_FEE_FRACTION = 0.0         # verified $0.00 on live maker fills 2026-07-15

# --- Edge thresholds (dollars per contract, after fees) ---
MAKER_EDGE_MIN = 0.02
TAKER_EDGE_MIN = 0.03
MAKER_ENABLED = True             # post-only resting quotes in the maker band
MAKER_MAX_RESTING = 3            # resting quotes at once (one per ticker+side)
# Humility band. PROVISIONAL: these numbers were fitted on CRYPTO cohorts and are
# inherited only as a placeholder. Weather legs commonly price 0.01-0.13, so a 15c
# floor rejects most of the book, and "fade the overpriced longshot" means BUYING
# the complement at 0.85-0.99 -- which an 85c ceiling blocks outright. Both must be
# re-derived from scripts/weather_edge_test.py before arming; the principled guard
# is edge-vs-model-uncertainty, not a raw price band. Left tight on purpose: while
# unfitted, they fail toward trading less.
MIN_BUY_PRICE = 0.15
MAX_BUY_PRICE = 0.85
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
STRATEGY_EPOCH = int(dt.datetime(2026, 7, 15, 23, 0, tzinfo=dt.UTC).timestamp())
                                 # Weather desk (w1) live at 2026-07-15 23:00Z.
                                 # The loss ledger follows the strategy VERSION —
                                 # the retired crypto book doesn't bill this one.
                                 # ALWAYS verify a new epoch with python and assert
                                 # it is in the PAST: a hand-typed epoch 23h in the
                                 # future once silently disabled the daily-loss rail
                                 # and only the test suite caught it.
MAX_OPEN_EXPOSURE = 12.00        # total cost basis of open positions + resting orders
ORDERS_PER_CYCLE = 2             # pacing: max new orders per scan cycle
STALE_DATA_HALT_S = 300          # halt if Deribit or Kalshi data older than this

# --- Engine cadence ---
# Temperature does not tick. Ensembles refresh ~every 6h, so a 5-minute scan is
# already ~70x faster than the information arrives. This slow clock is the whole
# structural reason weather can work where crypto could not.
REFRESH_S = 300
REFRESH_FINAL_HOUR_S = 120       # cycle time when nearest settle < FINAL_WINDOW_S away
FINAL_WINDOW_S = 3 * 3600
BASIS_ALARM_F = 3.0              # model median vs market center, ABSOLUTE degF.
                                 # The crypto relative-bps form would mean 0.09F
                                 # at 90F. 3F ~= 1.5 buckets ~= our lead-1d sd.
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
