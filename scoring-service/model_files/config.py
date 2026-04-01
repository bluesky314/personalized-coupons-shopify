import os
from datetime import date

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)

CREDENTIALS = os.path.join(PROJECT_DIR, 'fynd-jio-commerceml-prod-b0d5e00042cc.json')
OUTPUT_DIR  = os.path.join(BASE_DIR, 'outputs')
PLOTS_DIR   = os.path.join(OUTPUT_DIR, 'plots')
RAW_PARQUET = os.path.join(OUTPUT_DIR, 'raw_data.parquet')

# ── BigQuery ─────────────────────────────────────────────────────────────────
BQ_PROJECT  = 'fynd-jio-commerceml-prod'
BQ_LOCATION = 'asia-south1'

# ── Query window ──────────────────────────────────────────────────────────────
# Dates are computed dynamically relative to today so the pipeline always
# trains on the most recent data.  Override via env vars if needed:
#   QUERY_START=2024-06-01 QUERY_END=2026-02-01 python 01_extract.py
#
# Layout (all anchored to first of current month = last complete month):
#   QUERY_START  = 18 months before QUERY_END  (15m data + 3m burn-in)
#   DATA_START   = 15 months before QUERY_END  (first labeled training row)
#   TRAIN_END    =  5 months before QUERY_END  (~10 months train)
#   VAL_END      =  2 months before QUERY_END  (~3 months val, ~2 months test)

def _months_before(ref: date, n: int) -> date:
    month = ref.month - n
    year  = ref.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, 1)

_qend = date.today().replace(day=1)  # first of current month = last complete month

QUERY_END   = os.getenv('QUERY_END',   _qend.strftime('%Y-%m-%d'))
QUERY_START = os.getenv('QUERY_START', _months_before(_qend, 18).strftime('%Y-%m-%d'))
DATA_START  =                           _months_before(_qend, 15).strftime('%Y-%m-%d')
TRAIN_END   =                           _months_before(_qend,  5).strftime('%Y-%m-%d')
VAL_END     =                           _months_before(_qend,  2).strftime('%Y-%m-%d')

# ── Target ────────────────────────────────────────────────────────────────────
# 1 = no coupon/promotion discount applied → deal-agnostic customer
# 0 = coupon or promotion used             → deal-seeker
# Expected balance: ~55% positive (no coupon) → balanced; no scale_pos_weight needed.
TARGET = 'converted_without_coupon'

# ── Bayesian smoothing ────────────────────────────────────────────────────────
# Smoothed rate = (coupon_count + ALPHA * global_rate) / (n + ALPHA)
# ALPHA=20: pincode needs ~20 orders before its rate dominates the global prior.
BAYESIAN_ALPHA = 20

# ── Feature groups ────────────────────────────────────────────────────────────

FEATURES_ORDER = [
    'mrp_total',         # original listed price — available at cart time (before coupon)
    'order_total',       # amount paid after item-level markdown, before coupon — available at cart time
    'cart_item_count',   # number of bags in this order
    # cart_discount_pct removed: it's a derived ratio of mrp_total and order_total.
    # Giving the model both raw prices directly is strictly better — it can learn
    # the item markdown itself and is not fooled by low-discount carts.
]

# Point-in-time user coupon history (computed in 01_extract.py)
# All NaN for first-time buyers (total_prior_orders = 0) → imputed in preprocessing
FEATURES_HISTORY = [
    'total_prior_orders',
    'prior_orders_with_coupon',
    'prior_orders_without_coupon',
    'prior_coupon_use_rate',            # NaN for first-timers → -1 sentinel in preprocessing
    'eligible_coupon_use_rate',         # coupon rate on orders ≥ Rs.1500 only (strips eligibility bias)
                                        # NaN when no eligible prior orders → -1 sentinel
    'prior_eligible_orders',            # count of prior eligible orders (model knows confidence of above)
    'avg_coupon_value_used',            # avg coupon discount value across coupon orders
    'gross_lifetime_value',             # sum of prior order_totals
    'average_order_value',              # gross_lifetime_value / total_prior_orders
    'days_since_first_order',
    'days_since_last_order',
    'prior_cod_rate',
    'is_first_order',                   # 1 if total_prior_orders == 0
    # Engineered interaction features
    'coupon_rate_x_full_price',         # prior_coupon_use_rate × (1 − item_markdown_pct)
                                        # High = user applies coupons on full-price items → confirmed deal-seeker
                                        # Prevents model conflating "low item discount" with "deal-agnostic"
    'is_mixed_coupon_user',             # 1 if prior_coupon_use_rate in [0.20, 0.70] — explicit uncertainty flag
]

FEATURES_BEHAVIORAL = [
    'is_cod',          # 1 if this order is Cash on Delivery
    'order_dow',       # day of week (0=Mon ... 6=Sun)
    'order_hour',      # hour of day (0–23)
    # source_encoded dropped: ordering_source not available in dbe_bags; use company_id/sales_channel_id as proxies
    'company_id',      # brand / merchant ID (integer — tree splits handle high cardinality)
    'sales_channel_id',
]

# Bayesian-smoothed coupon usage rate per geo unit (computed on train only, no leakage)
FEATURES_GEO = [
    'pincode_coupon_rate',
    'city_coupon_rate',
    'state_coupon_rate',
]

ALL_FEATURES = (
    FEATURES_ORDER
    + FEATURES_HISTORY
    + FEATURES_BEHAVIORAL
    + FEATURES_GEO
)

# ── Coupon Optimization — Phase 2 ────────────────────────────────────────────
# Tier thresholds are calibrated by 03_segment_analysis.py.
# These defaults are overwritten by outputs/tier_calibration.json if present.
TIER_SUPPRESS_THRESHOLD  = 0.70   # P(deal_agnostic) > this → SUPPRESS (no coupon)
TIER_NUDGE_THRESHOLD     = 0.40   # P(deal_agnostic) > this → NUDGE, else FULL_COUPON
FULL_COUPON_RS           = 200    # Rs.200 reference value — kept for reference only, not used in amount logic
FULL_COUPON_PCT          = 0.25   # FULL_COUPON gives exactly 25% of basket (the cap IS the amount)
COUPON_PCT_CAP           = 0.25   # Hard ceiling: never recommend more than 25% of order_total

# NUDGE coupon personalisation — derived from Model 1 score only (Model 2 commented out).
# Formula: coupon_pct = NUDGE_PCT_SCALE * (1 - user_score), capped at COUPON_PCT_CAP.
# A confirmed deal-seeker (score=0.10) → 18% coupon.
# A borderline user  (score=0.60) →  8% coupon.
# Rationale: Model 2 (quantile regressor) was learning basket size, not user personality.
# The median coupon pct in training data was 11.1% across all basket sizes — a constant.
# Personalisation from Model 1 score is more defensible than a regression on order_total.
# Revisit when A/B test data on NUDGE conversion rates is available.
NUDGE_PCT_SCALE          = 0.20   # coupon_pct = 0.20 * (1 - score) → range [0.07, 0.20] within NUDGE band

# Amount regression config (06_coupon_amount_model.py)
# Regression trains on coupon-used orders only; target is coupon_pct = disc/order_total.
# order_total is NOT a feature here (it's the denominator of the target — division artefact).
AMOUNT_FEATURES_EXTRA    = []  # mrp_total and order_total already in ALL_FEATURES; no extras needed
AMOUNT_QUANTILES         = [0.25, 0.50]                  # p25 = conservative floor; p50 = median

# ── Personalised coupon strategies ───────────────────────────────────────────

# AOV Stretch: (aov_min, aov_max, stretch_target, coupon_pct)
# Logic: stretch % shrinks as AOV grows — big relative jumps only make sense at low spend levels.
# Minimum target is always Rs.1,000. At high AOV, stretch is ~30-40% above baseline, not 2x.
AOV_STRETCH_BRACKETS = [
    (0,    600,   1000,  0.10),   # AOV <600 → push to 1000 (floor)
    (600,  1000,  1500,  0.10),   # AOV 600–1000 → +50–900 gap, target 1500
    (1000, 1500,  2000,  0.12),   # AOV 1000–1500 → ~40% stretch
    (1500, 2500,  3500,  0.12),   # AOV 1500–2500 → ~40-60% stretch
    (2500, 4000,  5500,  0.12),   # AOV 2500–4000 → ~40% stretch
    (4000, 6000,  7500,  0.15),   # AOV 4000–6000 → ~30% stretch
    (6000, 9000,  10000, 0.15),   # AOV 6000–9000 → ~30% stretch
    (9000, 99999, 13000, 0.15),   # AOV 9000+     → ~30-45% stretch
]
AOV_STRETCH_MIN_GAP = 400   # don't show stretch if gap between current basket and target < Rs.400

# Lapse Reactivation: (days_min, coupon_pct, message)
LAPSE_REACTIVATION_TIERS = [
    (365, 0.20, "We miss you — 20% off, today only"),
    (180, 0.15, "It's been a while — 15% off, valid 48 hours"),
    (90,  0.10, "Come back — 10% off your next order"),
]
LAPSE_MIN_ORDERS   = 3     # don't reactivate true one-timers
LAPSE_EXPIRY_HOURS = 48

# Category Upgrade: applied when user is concentrated in one category
CATEGORY_UPGRADE_CONCENTRATION_THRESHOLD = 0.70  # ≥70% of past orders in one L1 category
CATEGORY_UPGRADE_MIN_ORDERS = 5
CATEGORY_UPGRADE_COUPON_PCT  = 0.15

# Loyalty Milestone: reward at order count milestones
# (min_orders, coupon_pct, label)
LOYALTY_MILESTONES = [
    (5,   0.08,  '5 orders'),
    (10,  0.10,  '10 orders'),
    (20,  0.12,  '20 orders'),
    (40,  0.15,  '40 orders'),
    (75,  0.18,  '75 orders'),
    (150, 0.20,  '150 orders'),
]
LOYALTY_MILESTONE_WINDOW = 2   # fire if user is within 2 orders of crossing a milestone

# Frequency Booster: user is ordering less frequently than their historical average
FREQUENCY_BOOSTER_MULTIPLIER = 1.5   # fire if days_since_last > avg_gap × 1.5
FREQUENCY_BOOSTER_MIN_ORDERS = 4     # need at least 4 orders to compute a meaningful avg gap
FREQUENCY_BOOSTER_COUPON_PCT  = 0.10

# Highest Basket Celebration: current basket exceeds user's historical max order
HIGHEST_BASKET_COUPON_PCT = 0.05     # flat 5% extra off

# ── Source encoding map ───────────────────────────────────────────────────────
SOURCE_MAP = {
    'storefront':    0,
    'store_os_pos':  1,
    'gofynd':        2,
    'kiosk':         3,
}
SOURCE_OTHER = 4

# ── Preprocessing caps ────────────────────────────────────────────────────────
ORDER_TOTAL_CAP = 100_000   # p99 cap on order_total

# ── Model config ──────────────────────────────────────────────────────────────
# Balanced classes (~55/45) — no scale_pos_weight; eval on logloss + ROC-AUC.

XGB_PARAMS = {
    'n_estimators':          2000,
    'learning_rate':         0.01,
    'max_depth':             6,
    'min_child_weight':      5,
    'subsample':             0.8,
    'colsample_bytree':      0.8,
    'gamma':                 0.1,
    'reg_alpha':             0.1,
    'reg_lambda':            1.0,
    'eval_metric':           'logloss',
    'early_stopping_rounds': 50,
    'random_state':          42,
    'n_jobs':                -1,
}

LGB_PARAMS = {
    'n_estimators':       2000,
    'learning_rate':      0.01,
    'max_depth':          6,
    'num_leaves':         63,
    'min_child_samples':  50,
    'subsample':          0.8,
    'subsample_freq':     1,
    'colsample_bytree':   0.8,
    'reg_alpha':          0.1,
    'reg_lambda':         1.0,
    'is_unbalance':       True,   # upweights minority class (deal-seekers) to correct 78% DA training skew
    'random_state':       42,
    'n_jobs':             -1,
    'verbose':            -1,
}
