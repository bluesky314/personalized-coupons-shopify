"""
Core prediction logic for the Coupon Propensity API.
Mirrors enclose/model_handler.py adapted for coupon use-case.

CouponPredictor loads all models at startup and exposes three scoring functions:
  - score_user_level()  → bag-blind deal-agnostic probability + tier
  - score_bag_aware()   → bag-aware deal-agnostic probability
  - predict_full()      → full two-model flow: tier + coupon % + risk factors
"""

import os
import sys
import json
import pickle
import logging
from uuid import uuid4
from typing import Optional

import numpy as np
import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE_DIR)

import config

# Import preprocessing from training scripts (single source of truth)
from importlib.util import spec_from_file_location, module_from_spec

def _load_module(name, path):
    spec = spec_from_file_location(name, path)
    mod  = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_train = _load_module("train",      os.path.join(_BASE_DIR, "02_train.py"))
_ulm   = _load_module("user_level", os.path.join(_BASE_DIR, "09_user_level_model.py"))

apply_preprocessing      = _train.apply_preprocessing
apply_user_preprocessing = _ulm.apply_user_preprocessing
USER_FEATURES            = _ulm.USER_FEATURES

log = logging.getLogger(__name__)


# ── Feature importances ───────────────────────────────────────────────────────

def _get_feature_importances(model, feature_names):
    try:
        if hasattr(model, 'calibrated_classifiers_'):
            base = model.calibrated_classifiers_[0].estimator
        else:
            base = model
        if hasattr(base, 'feature_importances_'):
            return dict(zip(feature_names, base.feature_importances_.tolist()))
    except Exception:
        pass
    return {}


# ── CouponPredictor — loaded once at startup ──────────────────────────────────

class CouponPredictor:
    def __init__(self, outputs_dir: str):
        self.outputs_dir     = outputs_dir
        self.user_model      = None
        self.bag_model       = None
        self.stats           = {}
        self.suppress_thr    = config.TIER_SUPPRESS_THRESHOLD
        self.nudge_thr       = config.TIER_NUDGE_THRESHOLD
        self.models_loaded   = {}
        self.user_importances = {}
        self.bag_importances  = {}

    def load(self):
        self._load_stats()
        self._load_user_model()
        self._load_bag_model()
        # Model 2 (amount model) intentionally not loaded.
        # NUDGE amount is derived from Model 1 score:
        #   coupon_pct = NUDGE_PCT_SCALE * (1 - user_score), capped at COUPON_PCT_CAP (25%)
        self._load_thresholds()
        log.info("CouponPredictor ready. Loaded: %s", self.models_loaded)

    def _load_stats(self):
        path = os.path.join(self.outputs_dir, 'preprocessor_stats.json')
        with open(path) as f:
            raw = json.load(f)
        self.stats = {
            k: {kk: float(vv) for kk, vv in v.items()} if isinstance(v, dict) else v
            for k, v in raw.items()
        }
        log.info("Stats loaded: %s", path)

    def _load_user_model(self):
        path = os.path.join(self.outputs_dir, 'model_user_level_lgb_cal.pkl')
        with open(path, 'rb') as f:
            self.user_model = pickle.load(f)
        self.models_loaded['user_level'] = path
        self.user_importances = _get_feature_importances(self.user_model, USER_FEATURES)
        log.info("User-level model loaded: %s", path)

    def _load_bag_model(self):
        path = os.path.join(self.outputs_dir, 'model_lgb_cal.pkl')
        if os.path.exists(path):
            with open(path, 'rb') as f:
                self.bag_model = pickle.load(f)
            self.models_loaded['bag_aware'] = path
            self.bag_importances = _get_feature_importances(self.bag_model, config.ALL_FEATURES)
            log.info("Bag-aware model loaded: %s", path)
        else:
            log.warning("Bag-aware model not found at %s", path)

    def _load_thresholds(self):
        cal_path = os.path.join(self.outputs_dir, 'tier_calibration.json')
        if os.path.exists(cal_path):
            with open(cal_path) as f:
                cal = json.load(f)
            self.suppress_thr = cal.get('suppress_threshold', self.suppress_thr)
            self.nudge_thr    = cal.get('nudge_threshold',    self.nudge_thr)
        log.info("Thresholds: SUPPRESS=%.3f  NUDGE=%.3f", self.suppress_thr, self.nudge_thr)


# ── Row builder ───────────────────────────────────────────────────────────────

def _build_row(data: dict) -> pd.DataFrame:
    cols = [
        'user_id', 'order_id',
        'mrp_total', 'order_total', 'cart_item_count',
        'is_cod', 'order_dow', 'order_hour', 'company_id', 'sales_channel_id',
        'total_prior_orders', 'prior_orders_with_coupon', 'prior_orders_without_coupon',
        'prior_coupon_use_rate', 'eligible_coupon_use_rate', 'prior_eligible_orders',
        'avg_coupon_value_used', 'gross_lifetime_value', 'average_order_value',
        'days_since_first_order', 'days_since_last_order',
        'prior_cod_rate', 'is_first_order',
        'delivery_pincode', 'delivery_city', 'delivery_state',
        'cart_discount_pct',
    ]
    row = {col: data.get(col, np.nan) for col in cols}
    mrp = data.get('mrp_total')
    ot  = data.get('order_total')
    if mrp and ot and mrp > 0:
        row['cart_discount_pct'] = max(0.0, min(1.0, 1.0 - ot / mrp))
    else:
        row['cart_discount_pct'] = 0.0
    return pd.DataFrame([row])


# ── Evidence confidence weighting ─────────────────────────────────────────────
#
# Problem: the model assigns similar scores regardless of order count.
# 1 order with 0% coupon and 30 orders with 0% coupon both score ~0.71 — but
# the confidence behind those two scores is completely different.
#
# Fix: ASYMMETRIC confidence weighting.
#
# For DA-leaning scores (raw_score > 0.5):
#   Pull toward 0.5 when evidence is thin — don't suppress someone just because
#   they have 1 order with no coupon history yet.
#   weighted = 0.5 + (raw_score - 0.5) × confidence
#
# For seeker-leaning scores (raw_score <= 0.5):
#   Trust the signal — a user with 2 orders at 100% coupon rate IS a deal-seeker.
#   Thin seeker evidence should stay seeker-side (push toward 0.5 from below only
#   when evidence is very thin, i.e. confidence < 0.25).
#   weighted = raw_score - (0.5 - raw_score) × max(0, 0.25 - confidence)
#            = effectively floor at raw_score, only nudge up very slightly if <2 orders
#
# Eligible rate override:
#   If eligible_coupon_use_rate diverges from raw rate by >0.20, use eligible rate
#   as the effective rate regardless of eligible order count — it's the truer signal.
#
# confidence = min(orders / orders_needed, 1.0)
# orders_needed: 8 (clear signal: rate=0% or 100%) → 20 (ambiguous: rate=50%)

def _evidence_confidence(orders: int, coupon_rate: float) -> float:
    """Returns a confidence scalar in [0, 1]."""
    if orders <= 0:
        return 0.0
    signal_strength = abs(coupon_rate - 0.5) * 2.0
    orders_needed = 8.0 + (1.0 - signal_strength) * 12.0
    return min(orders / orders_needed, 1.0)


def _apply_evidence_weight(raw_score: float, data: dict) -> float:
    """Applies asymmetric evidence confidence weighting to the raw model score."""
    orders = int(data.get('total_prior_orders') or 0)
    rate   = data.get('prior_coupon_use_rate')

    # Eligible rate override: use when it diverges substantially from raw rate
    elig_rate   = data.get('eligible_coupon_use_rate')
    elig_orders = int(data.get('prior_eligible_orders') or 0)
    if elig_rate is not None and elig_rate >= 0 and elig_rate > (rate or 0) + 0.20:
        effective_rate   = elig_rate
        effective_orders = elig_orders if elig_orders >= 1 else orders
    elif rate is not None and rate >= 0:
        effective_rate   = rate
        effective_orders = orders
    else:
        return raw_score  # no history — return as-is (hard gate handles first-timers)

    conf = _evidence_confidence(effective_orders, effective_rate)

    if raw_score > 0.5:
        # DA-leaning: pull toward 0.5 when evidence is thin
        return 0.5 + (raw_score - 0.5) * conf
    else:
        # Seeker-leaning: trust the signal; only nudge up very slightly if <2 orders of evidence
        nudge = max(0.0, 0.25 - conf)
        return raw_score + (0.5 - raw_score) * nudge


# ── Hard gates ────────────────────────────────────────────────────────────────

def _check_hard_gate(data: dict) -> bool:
    if data.get('is_first_order', 0) == 1:
        return True
    rate = data.get('prior_coupon_use_rate')
    if rate is not None and rate > 0.70:
        return True
    return False


# ── Archetype ─────────────────────────────────────────────────────────────────

def _get_archetype(data: dict) -> str:
    if data.get('is_first_order', 0) == 1 or not data.get('total_prior_orders'):
        return 'FIRST_TIMER'
    rate = data.get('prior_coupon_use_rate')
    if rate is None:
        return 'FIRST_TIMER'
    if rate == 0:
        return 'NEVER_COUPON'
    if rate <= 0.30:
        return 'LIGHT_COUPON'
    if rate <= 0.70:
        return 'MIXED'
    return 'HEAVY_COUPON'


# ── Risk factors (mirrors enclose/model_handler.py build_risk_factors) ────────

def _build_risk_factors(data: dict, archetype: str, tier: str, user_score: float) -> list:
    """
    Returns list of RiskFactor dicts, sorted by direction (INCREASES_RISK first)
    then by impact (HIGH > MEDIUM > LOW). Same deterministic pattern as enclose/.
    """
    factors = []
    rate = data.get('prior_coupon_use_rate') or 0.0
    elig = data.get('eligible_coupon_use_rate')
    orders = int(data.get('total_prior_orders') or 0)
    avg_val = data.get('avg_coupon_value_used') or 0.0
    glv = data.get('gross_lifetime_value') or 0.0

    # ── Coupon history ─────────────────────────────────────────────────────────
    if archetype == 'HEAVY_COUPON':
        factors.append({
            "factor":    "Coupon history",
            "impact":    "HIGH",
            "detail":    f"{rate:.0%} historical coupon rate — confirmed deal-seeker. Will not convert without a coupon.",
            "direction": "INCREASES_RISK",
        })
    elif archetype == 'MIXED':
        factors.append({
            "factor":    "Coupon history",
            "impact":    "MEDIUM",
            "detail":    f"{rate:.0%} historical coupon rate — uncertain. Sometimes uses coupons, sometimes doesn't.",
            "direction": "INCREASES_RISK",
        })
    elif archetype == 'LIGHT_COUPON':
        factors.append({
            "factor":    "Coupon history",
            "impact":    "MEDIUM",
            "detail":    f"{rate:.0%} historical coupon rate — rarely uses coupons but not zero.",
            "direction": "DECREASES_RISK",
        })
    elif archetype == 'NEVER_COUPON':
        factors.append({
            "factor":    "Coupon history",
            "impact":    "HIGH",
            "detail":    f"0% historical coupon rate across {orders} orders — confirmed deal-agnostic.",
            "direction": "DECREASES_RISK",
        })
    elif archetype == 'FIRST_TIMER':
        factors.append({
            "factor":    "New user",
            "impact":    "HIGH",
            "detail":    "No purchase history — always show full coupon to maximise first conversion.",
            "direction": "INCREASES_RISK",
        })

    # ── Eligibility-adjusted rate ──────────────────────────────────────────────
    if elig is not None and elig >= 0 and orders >= 3:
        if elig > rate + 0.15:
            factors.append({
                "factor":    "Eligibility-adjusted rate",
                "impact":    "MEDIUM",
                "detail":    f"Eligible-order coupon rate {elig:.0%} is higher than raw rate {rate:.0%} — small basket orders masked true deal-seeking.",
                "direction": "INCREASES_RISK",
            })

    # ── Geo coupon culture ─────────────────────────────────────────────────────
    pincode_rate = data.get('pincode_coupon_rate')
    city_rate    = data.get('city_coupon_rate')
    global_rate  = 0.219
    geo_rate     = pincode_rate or city_rate or global_rate
    city         = data.get('delivery_city', 'this area')
    if geo_rate > global_rate * 1.4:
        factors.append({
            "factor":    "Geography",
            "impact":    "MEDIUM",
            "detail":    f"{city} has {geo_rate:.0%} coupon usage rate — above national average ({global_rate:.0%}). High deal-seeking area.",
            "direction": "INCREASES_RISK",
        })
    elif geo_rate < global_rate * 0.7:
        factors.append({
            "factor":    "Geography",
            "impact":    "LOW",
            "detail":    f"{city} has {geo_rate:.0%} coupon usage rate — below national average. Low deal-seeking area.",
            "direction": "DECREASES_RISK",
        })

    # ── Tenure / loyalty ──────────────────────────────────────────────────────
    if orders >= 20 and rate < 0.10:
        factors.append({
            "factor":    "Loyal buyer",
            "impact":    "HIGH",
            "detail":    f"{orders} orders without needing coupons — highly loyal, no coupon required.",
            "direction": "DECREASES_RISK",
        })
    elif orders >= 10 and rate < 0.10:
        factors.append({
            "factor":    "Repeat buyer",
            "impact":    "MEDIUM",
            "detail":    f"{orders} prior orders, rarely uses coupons — likely converts without one.",
            "direction": "DECREASES_RISK",
        })

    # Sort: INCREASES_RISK first, then HIGH > MEDIUM > LOW within each direction
    impact_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    direction_order = {"INCREASES_RISK": 0, "DECREASES_RISK": 1}
    factors.sort(key=lambda f: (direction_order.get(f["direction"], 9), impact_order.get(f["impact"], 9)))

    return factors[:6]  # max 6 factors, same as enclose/


# ── Summary one-liner ─────────────────────────────────────────────────────────

def _build_summary(tier: str, archetype: str, user_score: float, coupon_pct: float, order_total: float) -> str:
    coupon_rs = round(coupon_pct * order_total / 50) * 50 if order_total > 0 else 0
    if tier == 'SUPPRESS':
        return f"Deal-agnostic user (score {user_score:.2f}) — no coupon needed to convert."
    if tier == 'FULL_COUPON':
        if archetype == 'FIRST_TIMER':
            return f"First-time buyer — show full coupon ({coupon_pct:.0%} / Rs.{coupon_rs:.0f}) to maximise first conversion."
        if archetype == 'HEAVY_COUPON':
            return f"Confirmed deal-seeker ({archetype}) — show full coupon ({coupon_pct:.0%} / Rs.{coupon_rs:.0f})."
        return f"Likely deal-seeker (score {user_score:.2f}) — show full coupon ({coupon_pct:.0%} / Rs.{coupon_rs:.0f})."
    return f"Borderline user (score {user_score:.2f}) — show nudge coupon ({coupon_pct:.0%} / Rs.{coupon_rs:.0f}) to tip conversion."


# ── Tier + amount ─────────────────────────────────────────────────────────────

def _assign_tier(score: float, hard_gated: bool, predictor: CouponPredictor) -> str:
    if hard_gated:
        return 'FULL_COUPON'
    if score >= predictor.suppress_thr:
        return 'SUPPRESS'
    if score >= predictor.nudge_thr:
        return 'NUDGE'
    return 'FULL_COUPON'


def _compute_coupon_amount(tier: str, order_total: float, user_score: float) -> tuple:
    """
    Returns (coupon_pct, coupon_rs).

    NUDGE personalisation formula (Model 1 score only — Model 2 commented out):
      coupon_pct = NUDGE_PCT_SCALE * (1 - user_score), capped at 25%
      score=0.10 → 18%  |  score=0.40 → 12%  |  score=0.64 → 7%

    FULL_COUPON: standard Rs.200 flat coupon, converted to pct for reference.
    """
    if tier == 'SUPPRESS':
        return 0.0, 0.0

    if tier == 'FULL_COUPON':
        # Full coupon = FULL_COUPON_PCT (25%) of basket, always. No flat Rs. amount.
        pct       = config.FULL_COUPON_PCT
        coupon_rs = round(pct * order_total / 50) * 50 if order_total > 0 else 0.0
        return round(pct, 4), float(max(coupon_rs, 50))

    # NUDGE — personalised from Model 1 score
    pct    = min(config.NUDGE_PCT_SCALE * (1.0 - user_score), config.COUPON_PCT_CAP)
    pct    = max(pct, 0.05)
    coupon_rs = max(int(np.round(pct * order_total / 50) * 50), 50) if order_total > 0 else 50
    return round(pct, 4), float(coupon_rs)


# ── Personalised strategies ───────────────────────────────────────────────────
#
# All three strategies are INDEPENDENT of the main tier decision.
# They apply to ALL users regardless of SUPPRESS / NUDGE / FULL_COUPON tier.
# The brand decides which strategy to activate per campaign.
#
# Strategies returned as a list in predict_full(). Empty list if none apply.

def _aov_stretch_strategy(data: dict) -> Optional[dict]:
    """
    Show a higher-% coupon that only activates if user hits a spend target
    meaningfully above their historical AOV. Goal: grow basket size.
    """
    aov         = float(data.get('average_order_value') or 0)
    order_total = float(data.get('order_total') or 0)
    if aov <= 0:
        return None

    bracket = None
    for aov_min, aov_max, target, pct in config.AOV_STRETCH_BRACKETS:
        if aov_min <= aov < aov_max:
            bracket = (target, pct)
            break
    if bracket is None:
        return None

    target_rs, coupon_pct = bracket

    # Don't show if current basket is already close to or above the target
    gap = target_rs - order_total
    if gap < config.AOV_STRETCH_MIN_GAP:
        return None

    coupon_rs = round(coupon_pct * target_rs / 50) * 50
    return {
        "strategy":      "AOV_STRETCH",
        "coupon_pct":    round(coupon_pct, 4),
        "coupon_rs":     float(coupon_rs),
        "min_basket_rs": float(target_rs),
        "message":       f"Add more to your cart — get {coupon_pct:.0%} off when you spend Rs.{int(target_rs):,}",
        "expiry_hours":  None,
    }


def _lapse_reactivation_strategy(data: dict) -> Optional[dict]:
    """
    Time-gated coupon for users who haven't ordered in a while.
    Scales with lapse depth. 48-hour expiry creates urgency.
    """
    days   = data.get('days_since_last_order')
    orders = int(data.get('total_prior_orders') or 0)

    if days is None or orders < config.LAPSE_MIN_ORDERS:
        return None

    days = float(days)
    matched = None
    for days_min, pct, msg in config.LAPSE_REACTIVATION_TIERS:
        if days >= days_min:
            matched = (pct, msg)
            break
    if matched is None:
        return None

    pct, msg = matched
    order_total = float(data.get('order_total') or 0)
    coupon_rs   = round(pct * order_total / 50) * 50 if order_total > 0 else 0.0

    return {
        "strategy":      "LAPSE_REACTIVATION",
        "coupon_pct":    round(pct, 4),
        "coupon_rs":     float(coupon_rs),
        "min_basket_rs": None,
        "message":       msg,
        "expiry_hours":  config.LAPSE_EXPIRY_HOURS,
    }


def _category_upgrade_strategy(data: dict) -> Optional[dict]:
    """
    Coupon restricted to a category the user has never bought in.
    Only fires when user is heavily concentrated in one category.
    Goal: expand purchase breadth → higher LTV.
    """
    orders          = int(data.get('total_prior_orders') or 0)
    concentration   = data.get('category_concentration')
    top_category    = data.get('top_category')

    if (orders < config.CATEGORY_UPGRADE_MIN_ORDERS
            or concentration is None
            or concentration < config.CATEGORY_UPGRADE_CONCENTRATION_THRESHOLD
            or not top_category):
        return None

    pct = config.CATEGORY_UPGRADE_COUPON_PCT
    order_total = float(data.get('order_total') or 0)
    coupon_rs   = round(pct * order_total / 50) * 50 if order_total > 0 else 0.0

    return {
        "strategy":      "CATEGORY_UPGRADE",
        "coupon_pct":    round(pct, 4),
        "coupon_rs":     float(coupon_rs),
        "min_basket_rs": None,
        "message":       f"You mostly buy {top_category} — get {pct:.0%} off your first order in a new category",
        "expiry_hours":  None,
    }


def _loyalty_milestone_strategy(data: dict) -> Optional[dict]:
    """
    Reward users who are close to (within LOYALTY_MILESTONE_WINDOW orders of) a
    significant order-count milestone. Coupon scales with milestone depth.
    """
    orders = int(data.get('total_prior_orders') or 0)
    if orders == 0:
        return None

    matched = None
    for milestone, pct, label in config.LOYALTY_MILESTONES:
        gap = milestone - orders
        if 0 < gap <= config.LOYALTY_MILESTONE_WINDOW:
            matched = (milestone, pct, label, gap)
            break
        if orders >= milestone:
            # check next milestone up
            continue

    if matched is None:
        return None

    milestone, pct, label, gap = matched
    order_total = float(data.get('order_total') or 0)
    coupon_rs   = round(pct * order_total / 50) * 50 if order_total > 0 else 0.0

    return {
        "strategy":      "LOYALTY_MILESTONE",
        "coupon_pct":    round(pct, 4),
        "coupon_rs":     float(coupon_rs),
        "min_basket_rs": None,
        "message":       f"You're only {gap} order{'s' if gap > 1 else ''} away from your {label} milestone — here's {pct:.0%} off to celebrate!",
        "expiry_hours":  None,
    }


def _frequency_booster_strategy(data: dict) -> Optional[dict]:
    """
    Fire when a user is ordering less frequently than their historical average.
    Goal: pull them back to their natural cadence before they churn.
    """
    orders         = int(data.get('total_prior_orders') or 0)
    days_since     = data.get('days_since_last_order')
    avg_gap        = data.get('avg_order_gap_days')

    if (orders < config.FREQUENCY_BOOSTER_MIN_ORDERS
            or days_since is None
            or avg_gap is None
            or avg_gap <= 0):
        return None

    days_since = float(days_since)
    avg_gap    = float(avg_gap)

    if days_since < avg_gap * config.FREQUENCY_BOOSTER_MULTIPLIER:
        return None

    pct         = config.FREQUENCY_BOOSTER_COUPON_PCT
    order_total = float(data.get('order_total') or 0)
    coupon_rs   = round(pct * order_total / 50) * 50 if order_total > 0 else 0.0
    drift_days  = int(days_since - avg_gap)

    return {
        "strategy":      "FREQUENCY_BOOSTER",
        "coupon_pct":    round(pct, 4),
        "coupon_rs":     float(coupon_rs),
        "min_basket_rs": None,
        "message":       f"You usually order every {int(avg_gap)} days — it's been {int(days_since)}. Here's {pct:.0%} off to get back on track.",
        "expiry_hours":  None,
    }


def _highest_basket_strategy(data: dict) -> Optional[dict]:
    """
    Celebrate when the current basket exceeds the user's historical max order.
    Flat 5% extra off — a reward for their biggest-ever spend.
    """
    order_total   = float(data.get('order_total') or 0)
    max_order_val = data.get('max_order_value')
    orders        = int(data.get('total_prior_orders') or 0)

    if order_total <= 0 or max_order_val is None or orders < 2:
        return None

    if order_total <= float(max_order_val):
        return None

    pct       = config.HIGHEST_BASKET_COUPON_PCT
    coupon_rs = round(pct * order_total / 50) * 50

    return {
        "strategy":      "HIGHEST_BASKET",
        "coupon_pct":    round(pct, 4),
        "coupon_rs":     float(coupon_rs),
        "min_basket_rs": None,
        "message":       f"Your biggest basket ever! 🎉 Here's an extra {pct:.0%} off — you've outdone yourself.",
        "expiry_hours":  None,
    }


def _build_personalized_strategies(data: dict) -> list:
    """Collect all applicable strategies. All are independent; brand picks which to activate."""
    strategies = []
    for fn in (
        _aov_stretch_strategy,
        _lapse_reactivation_strategy,
        _category_upgrade_strategy,
        _loyalty_milestone_strategy,
        _frequency_booster_strategy,
        _highest_basket_strategy,
    ):
        result = fn(data)
        if result is not None:
            strategies.append(result)
    return strategies


# ── Action label ──────────────────────────────────────────────────────────────

def _tier_to_action(tier: str) -> str:
    return {
        'SUPPRESS':    'SUPPRESS_COUPON',
        'NUDGE':       'SHOW_NUDGE_COUPON',
        'FULL_COUPON': 'SHOW_FULL_COUPON',
    }.get(tier, 'SHOW_FULL_COUPON')


# ── Public scoring functions ───────────────────────────────────────────────────

def score_user_level(data: dict, predictor: CouponPredictor) -> dict:
    archetype  = _get_archetype(data)
    hard_gated = _check_hard_gate(data)
    row_df     = _build_row(data)

    if hard_gated:
        user_score = 0.0
    else:
        X = apply_user_preprocessing(row_df, predictor.stats)
        raw_score  = float(predictor.user_model.predict_proba(X)[:, 1][0])
        user_score = _apply_evidence_weight(raw_score, data)

    tier = _assign_tier(user_score, hard_gated, predictor)

    return {
        'user_id':               data['user_id'],
        'deal_agnostic_score':   round(user_score, 4),
        'deal_seeker_probability': round(1.0 - user_score, 4),
        'tier':                  tier,
        'hard_gate_applied':     hard_gated,
        'model':                 'user_level',
        'archetype':             archetype,
        'request_id':            str(uuid4()),
    }


def score_bag_aware(data: dict, predictor: CouponPredictor) -> dict:
    hard_gated = _check_hard_gate(data)
    row_df     = _build_row(data)

    if hard_gated or predictor.bag_model is None:
        bag_score = 0.0
    else:
        X = apply_preprocessing(row_df, predictor.stats)
        bag_score = float(predictor.bag_model.predict_proba(X)[:, 1][0])

    tier = _assign_tier(bag_score, hard_gated, predictor)

    return {
        'user_id':               data['user_id'],
        'order_id':              data.get('order_id'),
        'deal_agnostic_score':   round(bag_score, 4),
        'deal_seeker_probability': round(1.0 - bag_score, 4),
        'tier':                  tier,
        'hard_gate_applied':     hard_gated,
        'model':                 'bag_aware',
        'request_id':            str(uuid4()),
    }


def predict_full(data: dict, predictor: CouponPredictor) -> dict:
    archetype  = _get_archetype(data)
    hard_gated = _check_hard_gate(data)
    row_df     = _build_row(data)

    # User-level score (primary — tier decision)
    if hard_gated:
        user_score = 0.0
    else:
        X_user = apply_user_preprocessing(row_df, predictor.stats)
        raw_score  = float(predictor.user_model.predict_proba(X_user)[:, 1][0])
        user_score = _apply_evidence_weight(raw_score, data)

    tier = _assign_tier(user_score, hard_gated, predictor)

    # Bag-aware score (informational only)
    bag_score = None
    if data.get('order_total') is not None and predictor.bag_model is not None:
        try:
            X_bag = apply_preprocessing(row_df, predictor.stats)
            bag_score = float(predictor.bag_model.predict_proba(X_bag)[:, 1][0])
        except Exception as e:
            log.warning("Bag-aware scoring failed: %s", e)

    # Coupon amount
    order_total = float(data.get('order_total') or 0)
    coupon_pct, coupon_rs = _compute_coupon_amount(tier, order_total, user_score)

    # Risk factors + summary
    risk_factors = _build_risk_factors(data, archetype, tier, user_score)
    summary      = _build_summary(tier, archetype, user_score, coupon_pct, order_total)

    # Personalised strategies — independent of tier, apply to all users
    strategies = _build_personalized_strategies(data)

    return {
        'user_id':                  data['user_id'],
        'order_id':                 data.get('order_id'),
        'tier':                     tier,
        'action':                   _tier_to_action(tier),
        'recommended_coupon_pct':   coupon_pct,
        'recommended_coupon_rs':    coupon_rs,
        'deal_agnostic_score':      round(user_score, 4),
        'hard_gate_applied':        hard_gated,
        'user_score':               round(user_score, 4),
        'bag_score':                round(bag_score, 4) if bag_score is not None else None,
        'summary':                  summary,
        'key_risk_factors':         risk_factors,
        'personalized_strategies':  strategies,
        'request_id':               str(uuid4()),
    }
