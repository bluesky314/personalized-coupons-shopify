#!/usr/bin/env python3
"""
Step 9 — User-Level Deal-Seeking Model (bag-blind).

Trains a LightGBM classifier using ONLY user history features — no bag features
(no mrp_total, order_total, cart_item_count). The hypothesis: deal-seeking is a
personality trait and should be scored independently of what the user is buying.

Contrast with 02_train.py (bag-aware model):
  - Bag-aware: confounds eligibility artifact with deal-seeking personality.
    Small-basket users look deal-agnostic because coupons rarely unlocked for them.
  - Bag-blind (this model): strips that artifact entirely. Same user gets the
    same score whether buying Rs. 500 or Rs. 5,000.

Features used:
  - eligible_coupon_use_rate   (coupon rate on orders >= Rs.1500 only)
  - prior_coupon_use_rate      (all orders, kept as context)
  - prior_eligible_orders      (confidence of above)
  - avg_coupon_value_used
  - total_prior_orders
  - days_since_first_order, days_since_last_order
  - gross_lifetime_value, average_order_value
  - prior_cod_rate
  - is_first_order
  - pincode_coupon_rate, city_coupon_rate, state_coupon_rate  (geo culture)
  - company_id                 (brand context — affects coupon culture)

NOT used (bag features — this is the key design decision):
  - mrp_total, order_total, cart_item_count
  - cart_discount_pct, coupon_rate_x_full_price, is_mixed_coupon_user

Usage:
    python 09_user_level_model.py

Outputs (in outputs/):
    model_user_level_lgb.pkl
    model_user_level_lgb_cal.pkl
    user_level_metrics.json
    plots/user_level_curves.png
"""

import os
import sys
import json
import pickle
import logging
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import (
    roc_auc_score, average_precision_score,
    precision_recall_curve, roc_curve,
)
import lightgbm as lgb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

from importlib.util import spec_from_file_location, module_from_spec
_spec = spec_from_file_location("train", os.path.join(os.path.dirname(__file__), "02_train.py"))
_train = module_from_spec(_spec)
_spec.loader.exec_module(_train)

load_data  = _train.load_data
split_data = _train.split_data

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(config.OUTPUT_DIR, exist_ok=True)
os.makedirs(config.PLOTS_DIR,  exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(config.OUTPUT_DIR, 'user_level_model.log'), mode='w'),
    ],
)
log = logging.getLogger(__name__)

# ── User-level feature set (bag-blind) ────────────────────────────────────────
USER_FEATURES = [
    # Eligibility-adjusted coupon rate (core signal — strips basket-size artifact)
    'eligible_coupon_use_rate',     # NaN → -1 sentinel
    'prior_eligible_orders',        # confidence weight for above

    # Raw coupon history (all orders)
    'prior_coupon_use_rate',        # NaN → -1 sentinel
    'prior_orders_with_coupon',
    'prior_orders_without_coupon',
    'avg_coupon_value_used',

    # Tenure and engagement
    'total_prior_orders',
    'days_since_first_order',
    'days_since_last_order',        # NaN → -1 sentinel
    'gross_lifetime_value',
    'average_order_value',
    'prior_cod_rate',
    'is_first_order',

    # Company (brand/merchant coupon culture)
    'company_id',

    # Geo coupon culture (Bayesian-smoothed from train only)
    'pincode_coupon_rate',
    'city_coupon_rate',
    'state_coupon_rate',
]


def fit_user_preprocessor(train_df: pd.DataFrame, stats_bag: dict) -> dict:
    """
    Build user-level preprocessing stats from train only.
    Reuses geo rate maps from the bag-aware preprocessor (stats_bag) to
    avoid recomputing Bayesian smoothed rates.
    """
    stats = {}

    # Sentinel fills
    stats['avg_coupon_value_used_median'] = float(train_df['avg_coupon_value_used'].median())
    stats['average_order_value_median']   = float(train_df['average_order_value'].median())

    # Days-since-last-order median for returning customers
    returning = train_df[train_df['is_first_order'] == 0]
    stats['days_since_last_order_median'] = float(returning['days_since_last_order'].median())

    # Reuse geo maps and global rate from bag preprocessor
    for key in ['pincode_coupon_rate', 'city_coupon_rate', 'state_coupon_rate', 'global_coupon_rate']:
        if key in stats_bag:
            stats[key] = stats_bag[key]

    stats['company_id_median'] = float(train_df['company_id'].median())

    return stats


def apply_user_preprocessing(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    """Apply user-level preprocessing. Returns DataFrame with USER_FEATURES columns."""
    df = df.copy()

    # Numeric casts
    for col in ['company_id', 'total_prior_orders', 'prior_orders_with_coupon',
                'prior_orders_without_coupon', 'prior_eligible_orders']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    # Sentinel fills for NaN features
    df['prior_coupon_use_rate']    = df['prior_coupon_use_rate'].fillna(-1.0)
    df['days_since_last_order']    = df['days_since_last_order'].fillna(-1.0)
    df['avg_coupon_value_used']    = df['avg_coupon_value_used'].fillna(0.0)
    df['average_order_value']      = df['average_order_value'].fillna(
                                         stats.get('average_order_value_median', 0.0))
    df['gross_lifetime_value']     = df['gross_lifetime_value'].fillna(0.0)

    # eligible_coupon_use_rate: NaN → -1 (no eligible orders in history)
    if 'eligible_coupon_use_rate' not in df.columns:
        df['eligible_coupon_use_rate'] = -1.0
        df['prior_eligible_orders']    = 0.0
    else:
        df['eligible_coupon_use_rate'] = df['eligible_coupon_use_rate'].fillna(-1.0)
        df['prior_eligible_orders']    = df.get('prior_eligible_orders',
                                                pd.Series(0.0, index=df.index)).fillna(0.0)

    # Geo rates
    global_rate = stats.get('global_coupon_rate', 0.22)
    for geo_col, feat_name in [
        ('delivery_pincode', 'pincode_coupon_rate'),
        ('delivery_city',    'city_coupon_rate'),
        ('delivery_state',   'state_coupon_rate'),
    ]:
        rate_map = stats.get(feat_name, {})
        if geo_col in df.columns and rate_map:
            df[feat_name] = df[geo_col].map(rate_map).fillna(global_rate)
        else:
            df[feat_name] = global_rate

    df['company_id'] = df['company_id'].fillna(stats.get('company_id_median', 0.0))

    # Return only USER_FEATURES columns that exist
    feats = [f for f in USER_FEATURES if f in df.columns]
    return df[feats].astype(float)


def train_user_model(X_train, y_train, X_val, y_val):
    params = dict(config.LGB_PARAMS)
    # Remove is_unbalance — we'll use scale_pos_weight for explicit control
    params.pop('is_unbalance', None)
    n_pos = int((y_train == 1).sum())
    n_neg = int((y_train == 0).sum())
    # Don't use is_unbalance here since user-level distribution may differ
    params['is_unbalance'] = True
    log.info("Training user-level LightGBM (bag-blind): n_DA=%s  n_seeker=%s",
             f"{n_pos:,}", f"{n_neg:,}")
    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        eval_metric='binary_logloss',
        callbacks=[
            lgb.early_stopping(stopping_rounds=50, verbose=False),
            lgb.log_evaluation(period=50),
        ],
    )
    log.info("  Best iteration: %d", model.best_iteration_)
    return model


def eval_model(name, scores, labels, split_name):
    roc_auc = roc_auc_score(labels, scores)
    pr_auc  = average_precision_score(labels, scores)
    log.info("  %-35s [%s]  ROC-AUC=%.4f  PR-AUC=%.4f  n=%s",
             name, split_name, roc_auc, pr_auc, f"{len(labels):,}")

    prec, rec, thrs = precision_recall_curve(labels, scores)
    f1s = 2 * prec * rec / (prec + rec + 1e-9)
    best_idx = f1s.argmax()
    log.info("    %10s  %10s  %8s  %6s  %9s", "Threshold", "Precision", "Recall", "F1", "Flagged")
    log.info("    " + "-"*50)
    for thr in [0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70]:
        pred = (scores >= thr).astype(int)
        tp = ((pred==1) & (labels==1)).sum()
        fp = ((pred==1) & (labels==0)).sum()
        fn = ((pred==0) & (labels==1)).sum()
        flagged = tp + fp
        p = tp/flagged if flagged > 0 else 0.0
        r = tp/(tp+fn)  if (tp+fn) > 0 else 0.0
        f1 = 2*p*r/(p+r) if (p+r) > 0 else 0.0
        log.info("    %10.2f  %9.1f%%  %7.1f%%  %6.3f  %9s",
                 thr, p*100, r*100, f1, f"{flagged:,}")
    bt = float(thrs[best_idx-1]) if best_idx > 0 else 0.0
    log.info("    Best F1=%.4f at thr=%.3f", f1s[best_idx], bt)
    return {'roc_auc': round(roc_auc, 4), 'pr_auc': round(pr_auc, 4)}


def segment_analysis(scores, df, labels, model_name):
    """Suppress rate and actual DA rate by coupon-use bucket."""
    log.info("")
    log.info("=== SEGMENT ANALYSIS: %s ===", model_name)
    df = df.copy()
    df['_score']  = scores
    df['_actual'] = labels

    suppress_thr = 0.65
    nudge_thr    = 0.384

    buckets = [
        ('NEVER_COUPON  (rate=0)',         df['prior_coupon_use_rate'].fillna(-1) == 0),
        ('HEAVY_COUPON  (rate>0.70)',       df['prior_coupon_use_rate'].fillna(-1) > 0.70),
        ('MIXED         (rate 0.30–0.60)',  df['prior_coupon_use_rate'].fillna(-1).between(0.30, 0.60)),
        ('FIRST_TIMER   (no history)',      df['is_first_order'] == 1),
        ('LOW_BASKET    (order<1500)',      df['order_total'] < 1500),
    ]

    log.info("  %-35s  %8s  %8s  %8s  %8s  %8s",
             "Segment", "N", "Actual_DA", "SUPPRESS%", "NUDGE%", "FULL%")
    log.info("  " + "-"*80)
    for seg_name, mask in buckets:
        sub = df[mask]
        if len(sub) < 10:
            continue
        s = sub['_score']
        actual_da   = sub['_actual'].mean()
        supp_pct    = (s >= suppress_thr).mean()
        nudge_pct   = s.between(nudge_thr, suppress_thr).mean()
        full_pct    = (s < nudge_thr).mean()
        log.info("  %-35s  %8s  %7.1f%%  %7.1f%%  %7.1f%%  %7.1f%%",
                 seg_name, f"{len(sub):,}", actual_da*100,
                 supp_pct*100, nudge_pct*100, full_pct*100)


def main():
    log.info("=" * 60)
    log.info("  User-Level Deal-Seeking Model (Bag-Blind)")
    log.info("=" * 60)

    df = load_data()
    train_df, val_df, test_df = split_data(df)

    # Fit bag-aware preprocessor on train (reuse geo maps)
    stats_bag = _train.fit_preprocessor(train_df)

    # Fit user-level preprocessor
    stats_user = fit_user_preprocessor(train_df, stats_bag)

    X_train = apply_user_preprocessing(train_df, stats_user)
    X_val   = apply_user_preprocessing(val_df,   stats_user)
    X_test  = apply_user_preprocessing(test_df,  stats_user)

    y_train = train_df[config.TARGET].values
    y_val   = val_df[config.TARGET].values
    y_test  = test_df[config.TARGET].values

    log.info("Features: %d  %s", len(X_train.columns), list(X_train.columns))
    log.info("Train=%s  Val=%s  Test=%s", f"{len(y_train):,}", f"{len(y_val):,}", f"{len(y_test):,}")

    # ── Train ─────────────────────────────────────────────────────────────────
    model = train_user_model(X_train, y_train, X_val, y_val)

    with open(os.path.join(config.OUTPUT_DIR, 'model_user_level_lgb.pkl'), 'wb') as f:
        pickle.dump(model, f)

    # Platt calibration
    from sklearn.calibration import CalibratedClassifierCV
    cal = CalibratedClassifierCV(model, cv='prefit', method='sigmoid')
    cal.fit(X_val, y_val)
    with open(os.path.join(config.OUTPUT_DIR, 'model_user_level_lgb_cal.pkl'), 'wb') as f:
        pickle.dump(cal, f)
    log.info("Models saved.")

    # ── Evaluate ──────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("  EVALUATION")
    log.info("=" * 60)
    val_s  = cal.predict_proba(X_val)[:, 1]
    test_s = cal.predict_proba(X_test)[:, 1]

    val_metrics  = eval_model("UserLevel LGB+Platt", val_s,  y_val,  'val')
    test_metrics = eval_model("UserLevel LGB+Platt", test_s, y_test, 'test')

    # ── Segment analysis ──────────────────────────────────────────────────────
    segment_analysis(test_s, test_df, y_test, "UserLevel (bag-blind)")

    # ── Feature importance ────────────────────────────────────────────────────
    log.info("")
    log.info("=== FEATURE IMPORTANCE ===")
    imp = pd.Series(model.feature_importances_, index=X_train.columns).sort_values(ascending=False)
    for feat, val in imp.items():
        log.info("  %-35s  %.4f", feat, val)

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    prec, rec, _ = precision_recall_curve(y_test, test_s)
    fpr, tpr, _  = roc_curve(y_test, test_s)
    axes[0].plot(rec, prec, label=f"UserLevel PR-AUC={average_precision_score(y_test, test_s):.4f}", lw=2)
    axes[0].axhline(y_test.mean(), color='k', linestyle='--', label='Random')
    axes[0].set(xlabel='Recall', ylabel='Precision', title='PR Curve — User-Level Model (TEST)',
                xlim=[0,1], ylim=[0,1])
    axes[0].legend(); axes[0].grid(alpha=0.3)
    axes[1].plot(fpr, tpr, label=f"UserLevel ROC-AUC={roc_auc_score(y_test, test_s):.4f}", lw=2)
    axes[1].plot([0,1],[0,1],'k--', label='Random')
    axes[1].set(xlabel='FPR', ylabel='TPR', title='ROC Curve — User-Level Model (TEST)',
                xlim=[0,1], ylim=[0,1])
    axes[1].legend(); axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(config.PLOTS_DIR, 'user_level_curves.png'), dpi=150, bbox_inches='tight')
    plt.close(fig)
    log.info("Plot saved.")

    # ── Save metrics ──────────────────────────────────────────────────────────
    metrics = {
        'model': 'user_level_lgb_cal',
        'features': list(X_train.columns),
        'val':  val_metrics,
        'test': test_metrics,
        'note': 'Bag-blind model. No mrp_total/order_total/cart_item_count. '
                'Strips eligibility artifact. Compare with 02_train.py bag-aware model.',
    }
    with open(os.path.join(config.OUTPUT_DIR, 'user_level_metrics.json'), 'w') as f:
        json.dump(metrics, f, indent=2)

    log.info("=" * 60)
    log.info("Done. Now run 10_compare_models.py for head-to-head analysis.")


if __name__ == '__main__':
    main()
