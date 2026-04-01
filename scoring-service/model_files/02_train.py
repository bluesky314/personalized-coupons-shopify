#!/usr/bin/env python3
"""
Step 2 — Train XGBoost and LightGBM for Discount Propensity Modeling.

Target: converted_without_coupon  (1 = bought without coupon → deal-agnostic)
Primary metric: ROC-AUC (classes are balanced ~55/45; no scale_pos_weight needed).
Secondary:      PR-AUC, best-F1 threshold table.

Pipeline:
  load_data() → split_data() → fit_preprocessor() → apply_preprocessing()
  → train XGBoost (early stopping on val logloss)
  → train LightGBM (early stopping on val binary_logloss)
  → Platt calibration on val set
  → evaluate on val + test; segment by is_first_order

Usage:
    python 02_train.py

Outputs (in outputs/):
    preprocessor_stats.json
    model_xgb.json, model_lgb.pkl
    model_xgb_cal.pkl, model_lgb_cal.pkl
    metrics.json
    plots/pr_curves.png, plots/roc_curves.png
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
    average_precision_score, roc_auc_score,
    precision_recall_curve, roc_curve,
)
import xgboost as xgb
import lightgbm as lgb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(config.OUTPUT_DIR,  exist_ok=True)
os.makedirs(config.PLOTS_DIR,   exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(config.OUTPUT_DIR, 'train.log'), mode='w'),
    ],
)
log = logging.getLogger(__name__)


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data() -> pd.DataFrame:
    """Load parquet, cast columns, filter to DATA_START (post burn-in)."""
    if not os.path.exists(config.RAW_PARQUET):
        log.error("Parquet not found: %s — run 01_extract.py first.", config.RAW_PARQUET)
        sys.exit(1)

    df = pd.read_parquet(config.RAW_PARQUET)
    df['created_ts'] = pd.to_datetime(df['created_ts'], utc=True)
    df[config.TARGET] = pd.to_numeric(df[config.TARGET], errors='coerce').fillna(0).astype(int)

    # Drop burn-in rows (Oct–Dec 2023); keep only data from DATA_START
    cutoff = pd.Timestamp(config.DATA_START, tz='UTC')
    before = len(df)
    df = df[df['created_ts'] >= cutoff].reset_index(drop=True)
    log.info("load_data: %s rows (dropped %s burn-in rows)", f"{len(df):,}", f"{before - len(df):,}")
    return df


def split_data(df: pd.DataFrame):
    """Chronological train / val / test split."""
    t_end = pd.Timestamp(config.TRAIN_END, tz='UTC')
    v_end = pd.Timestamp(config.VAL_END,   tz='UTC')

    train_df = df[df['created_ts'] <  t_end].copy()
    val_df   = df[(df['created_ts'] >= t_end) & (df['created_ts'] < v_end)].copy()
    test_df  = df[df['created_ts'] >= v_end].copy()

    log.info("Split: train=%s  val=%s  test=%s",
             f"{len(train_df):,}", f"{len(val_df):,}", f"{len(test_df):,}")
    for name, part in [('train', train_df), ('val', val_df), ('test', test_df)]:
        rate = part[config.TARGET].mean()
        log.info("  %s  deal-agnostic rate = %.2f%%", name, 100 * rate)
    return train_df, val_df, test_df


# ── Preprocessing ─────────────────────────────────────────────────────────────

def fit_preprocessor(train_df: pd.DataFrame) -> dict:
    """
    Compute all preprocessing statistics from the TRAINING set only.
    Never look at val/test here — that would cause leakage.
    """
    stats = {}

    # ── Order total cap (p99 of train) ────────────────────────────────────────
    stats['order_total_cap'] = float(
        min(train_df['order_total'].quantile(0.99), config.ORDER_TOTAL_CAP)
    )

    # ── mrp_total cap (p99 of train) ──────────────────────────────────────────
    stats['mrp_total_cap'] = float(
        min(train_df['mrp_total'].quantile(0.99), config.ORDER_TOTAL_CAP * 2)
    )

    # ── Median fill for features that can have NaN ────────────────────────────
    for feat in ['average_order_value', 'gross_lifetime_value', 'avg_coupon_value_used']:
        stats[f'{feat}_median'] = float(train_df[feat].median())

    # ── Bayesian-smoothed geo coupon usage rates ───────────────────────────────
    # Coupon usage = 1 - converted_without_coupon
    train_df = train_df.copy()
    train_df['_coupon_used'] = 1 - train_df[config.TARGET]
    global_coupon_rate = float(train_df['_coupon_used'].mean())
    stats['global_coupon_rate'] = global_coupon_rate
    alpha = config.BAYESIAN_ALPHA

    for geo_col, feat_name in [
        ('delivery_pincode', 'pincode_coupon_rate'),
        ('delivery_city',    'city_coupon_rate'),
        ('delivery_state',   'state_coupon_rate'),
    ]:
        if geo_col not in train_df.columns:
            stats[feat_name] = {}
            continue
        grp = train_df.groupby(geo_col)['_coupon_used'].agg(['sum', 'count'])
        smoothed = (grp['sum'] + alpha * global_coupon_rate) / (grp['count'] + alpha)
        stats[feat_name] = smoothed.to_dict()
        log.info("  Bayesian %s: %d unique values (global_coupon_rate=%.3f)",
                 feat_name, len(smoothed), global_coupon_rate)

    # ── company_id and sales_channel_id median (for unseen value imputation) ──
    stats['company_id_median']       = float(train_df['company_id'].median())
    stats['sales_channel_id_median'] = float(train_df['sales_channel_id'].median())

    # ── days_since_last_order median for returning customers ──────────────────
    returning = train_df[train_df['is_first_order'] == 0]
    stats['days_since_last_order_median'] = float(returning['days_since_last_order'].median())

    # Save stats
    stats_path = os.path.join(config.OUTPUT_DIR, 'preprocessor_stats.json')
    with open(stats_path, 'w') as f:
        # Convert dict-of-dicts values to JSON-serialisable format
        serialisable = {}
        for k, v in stats.items():
            if isinstance(v, dict):
                serialisable[k] = {str(kk): float(vv) for kk, vv in v.items()}
            else:
                serialisable[k] = v
        json.dump(serialisable, f, indent=2)
    log.info("  Preprocessor stats saved → %s", stats_path)

    return stats


def apply_preprocessing(df: pd.DataFrame, stats: dict) -> pd.DataFrame:
    """Apply preprocessing stats to any split.  Returns a DataFrame of ALL_FEATURES."""
    df = df.copy()

    # ── Cap order_total and mrp_total ─────────────────────────────────────────
    df['order_total'] = df['order_total'].clip(upper=stats['order_total_cap'])
    df['mrp_total']   = df['mrp_total'].clip(upper=stats.get('mrp_total_cap', stats['order_total_cap'] * 2))

    # ── cart_discount_pct retained for engineered features below ─────────────
    # (kept in raw data but NOT in ALL_FEATURES — used only to compute interactions)
    cart_disc = df['cart_discount_pct'].fillna(0.0).clip(0.0, 1.0) if 'cart_discount_pct' in df.columns \
                else (1.0 - (df['order_total'] / df['mrp_total'].replace(0, np.nan)).fillna(1.0)).clip(0.0, 1.0)

    # ── prior_coupon_use_rate: NaN → -1 (explicit first-timer sentinel) ───────
    df['prior_coupon_use_rate'] = df['prior_coupon_use_rate'].fillna(-1.0)

    # ── eligible_coupon_use_rate: NaN → -1 sentinel (no eligible prior orders) ─
    # Strips eligibility bias: only counts orders ≥ Rs.1500 where coupons were active
    if 'eligible_coupon_use_rate' not in df.columns:
        df['eligible_coupon_use_rate'] = -1.0   # backward-compat for old parquet
        df['prior_eligible_orders']    = 0.0
    else:
        df['eligible_coupon_use_rate'] = df['eligible_coupon_use_rate'].fillna(-1.0)
        df['prior_eligible_orders']    = df['prior_eligible_orders'].fillna(0.0)

    # ── Median fill ───────────────────────────────────────────────────────────
    df['average_order_value']   = df['average_order_value'].fillna(stats['average_order_value_median'])
    df['gross_lifetime_value']  = df['gross_lifetime_value'].fillna(0.0)
    df['avg_coupon_value_used'] = df['avg_coupon_value_used'].fillna(0.0)

    # ── days_since_last_order: NaN for first-timers → -1 sentinel ────────────
    df['days_since_last_order'] = df['days_since_last_order'].fillna(-1.0)

    # ── Geo Bayesian coupon rates ─────────────────────────────────────────────
    global_rate = stats['global_coupon_rate']
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

    # ── company_id / sales_channel_id fill ────────────────────────────────────
    df['company_id']       = df['company_id'].fillna(stats['company_id_median'])
    df['sales_channel_id'] = df['sales_channel_id'].fillna(stats['sales_channel_id_median'])

    # ── Engineered interaction features ───────────────────────────────────────
    # coupon_rate_x_full_price: high when user frequently uses coupons on full-price items.
    # Fixes the confound where low cart_discount_pct was incorrectly driving high DA scores.
    # For first-timers (prior_coupon_use_rate == -1), treat as 0 (no coupon history → no interaction).
    coupon_rate_clean = df['prior_coupon_use_rate'].clip(lower=0.0)  # -1 sentinel → 0
    item_markdown_pct = cart_disc  # fraction of MRP already discounted at item level
    df['coupon_rate_x_full_price'] = coupon_rate_clean * (1.0 - item_markdown_pct)

    # is_mixed_coupon_user: explicit flag for the ambiguous 20–70% band.
    # Model gets a direct signal that this user's coupon behavior is uncertain.
    df['is_mixed_coupon_user'] = (
        (df['prior_coupon_use_rate'] >= 0.20) &
        (df['prior_coupon_use_rate'] <= 0.70)
    ).astype(float)

    # ── Ensure all features are float ─────────────────────────────────────────
    result = df[config.ALL_FEATURES].astype(float)
    return result


# ── Training ──────────────────────────────────────────────────────────────────

def train_xgboost(X_train, y_train, X_val, y_val):
    params = dict(config.XGB_PARAMS)
    # Correct for class imbalance: upweight deal-seekers (class 0) relative to DA (class 1)
    n_pos = int((y_train == 1).sum())  # deal-agnostic (majority in train)
    n_neg = int((y_train == 0).sum())  # deal-seeker (minority in train)
    params['scale_pos_weight'] = n_neg / n_pos  # ~0.28 — downweights majority class
    log.info("Training XGBoost: scale_pos_weight=%.3f (n_DA=%s, n_seeker=%s)",
             params['scale_pos_weight'], f"{n_pos:,}", f"{n_neg:,}")
    model = xgb.XGBClassifier(**params)
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=50,
    )
    log.info("  XGBoost best_iteration = %d", model.best_iteration)
    return model


def train_lightgbm(X_train, y_train, X_val, y_val):
    params = dict(config.LGB_PARAMS)
    log.info("Training LightGBM: %s", params)
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
    log.info("  LightGBM best_iteration = %d", model.best_iteration_)
    return model


def calibrate_model(model, X_val, y_val):
    cal = CalibratedClassifierCV(model, cv='prefit', method='sigmoid')
    cal.fit(X_val, y_val)
    return cal


# ── Evaluation ────────────────────────────────────────────────────────────────

def eval_model(name, scores, labels, split_name):
    """Print ROC-AUC, PR-AUC, and threshold table."""
    roc_auc = roc_auc_score(labels, scores)
    pr_auc  = average_precision_score(labels, scores)
    pos_rate = labels.mean()

    log.info("")
    log.info("  %-30s [%s]  n=%s  pos_rate=%.1f%%",
             name, split_name, f"{len(labels):,}", 100 * pos_rate)
    log.info("    ROC-AUC = %.4f   PR-AUC = %.4f", roc_auc, pr_auc)

    # Threshold table (relevant range for balanced classes)
    prec, rec, thrs = precision_recall_curve(labels, scores)
    f1s = 2 * prec * rec / (prec + rec + 1e-9)
    best_idx = f1s.argmax()

    log.info("    %10s  %10s  %8s  %6s  %9s", "Threshold", "Precision", "Recall", "F1", "Flagged")
    log.info("    " + "-" * 50)
    for thr in [0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70]:
        pred    = (scores >= thr).astype(int)
        tp = ((pred == 1) & (labels == 1)).sum()
        fp = ((pred == 1) & (labels == 0)).sum()
        fn = ((pred == 0) & (labels == 1)).sum()
        flagged = tp + fp
        p = tp / flagged if flagged > 0 else 0.0
        r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2*p*r/(p+r) if (p+r) > 0 else 0.0
        log.info("    %10.2f  %9.1f%%  %7.1f%%  %6.3f  %9s",
                 thr, p*100, r*100, f1, f"{flagged:,}")

    bt = float(thrs[best_idx - 1]) if best_idx > 0 else 0.0
    log.info("    Best F1=%.4f at thr=%.3f  (P=%.1f%%  R=%.1f%%)",
             f1s[best_idx], bt, prec[best_idx]*100, rec[best_idx]*100)

    return {'roc_auc': round(roc_auc, 4), 'pr_auc': round(pr_auc, 4)}


def eval_by_segment(name, scores, labels, first_order_mask, split_name):
    """Evaluate separately for first-timers vs returning customers."""
    for seg_name, mask in [
        ('first_order',  first_order_mask),
        ('returning',    ~first_order_mask),
    ]:
        if mask.sum() == 0 or labels[mask].sum() == 0:
            continue
        eval_model(f"{name} | {seg_name}", scores[mask], labels[mask], split_name)


def baseline_majority(y_test):
    """Predict majority class (deal-agnostic) for all orders."""
    pos_rate = y_test.mean()
    return np.full(len(y_test), pos_rate)   # constant score = base rate


# ── Plots ─────────────────────────────────────────────────────────────────────

def save_roc_pr_curves(curves_data: dict, title_suffix: str, filename: str, y_true):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    for name, scores in curves_data.items():
        prec, rec, _ = precision_recall_curve(y_true, scores)
        pr_auc = average_precision_score(y_true, scores)
        fpr, tpr, _ = roc_curve(y_true, scores)
        roc_auc = roc_auc_score(y_true, scores)

        axes[0].plot(rec, prec, label=f"{name} PR-AUC={pr_auc:.4f}", linewidth=2)
        axes[1].plot(fpr, tpr, label=f"{name} ROC-AUC={roc_auc:.4f}", linewidth=2)

    pos_rate = y_true.mean()
    axes[0].axhline(pos_rate, color='black', linestyle='--', linewidth=1,
                    label=f"Random PR-AUC={pos_rate:.4f}")
    axes[1].plot([0, 1], [0, 1], 'k--', linewidth=1, label="Random ROC-AUC=0.5")

    axes[0].set(xlabel='Recall', ylabel='Precision',
                title=f'PR Curve — {title_suffix}',
                xlim=[0,1], ylim=[0,1])
    axes[1].set(xlabel='FPR', ylabel='TPR',
                title=f'ROC Curve — {title_suffix}',
                xlim=[0,1], ylim=[0,1])
    for ax in axes:
        ax.legend(fontsize=8); ax.grid(alpha=0.3)

    fig.tight_layout()
    plot_path = os.path.join(config.PLOTS_DIR, filename)
    fig.savefig(plot_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    log.info("  Curves saved → %s", plot_path)


# ── Feature importance ────────────────────────────────────────────────────────

def log_feature_importance(model, feature_names, model_name, top_n=20):
    if hasattr(model, 'feature_importances_'):
        imp = pd.Series(model.feature_importances_, index=feature_names).sort_values(ascending=False)
    elif hasattr(model, 'calibrated_classifiers_'):
        base = model.calibrated_classifiers_[0].estimator
        imp = pd.Series(base.feature_importances_, index=feature_names).sort_values(ascending=False)
    else:
        return

    log.info("  %s — Top %d features:", model_name, top_n)
    for feat, val in imp.head(top_n).items():
        log.info("    %-35s  %.4f", feat, val)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("  Discount Propensity — Training Pipeline")
    log.info("=" * 60)

    # ── Load & split ──────────────────────────────────────────────────────────
    df = load_data()
    train_df, val_df, test_df = split_data(df)

    # ── Fit preprocessor on train only ────────────────────────────────────────
    stats = fit_preprocessor(train_df)

    X_train = apply_preprocessing(train_df, stats)
    X_val   = apply_preprocessing(val_df,   stats)
    X_test  = apply_preprocessing(test_df,  stats)

    y_train = train_df[config.TARGET].values
    y_val   = val_df[config.TARGET].values
    y_test  = test_df[config.TARGET].values

    first_order_val  = val_df['is_first_order'].astype(bool).values
    first_order_test = test_df['is_first_order'].astype(bool).values

    log.info("Feature matrix: train=%s  val=%s  test=%s  features=%d",
             X_train.shape, X_val.shape, X_test.shape, len(config.ALL_FEATURES))

    # ── Baseline ──────────────────────────────────────────────────────────────
    b_test = baseline_majority(y_test)

    # ── XGBoost ───────────────────────────────────────────────────────────────
    log.info("─" * 60)
    xgb_model = train_xgboost(X_train, y_train, X_val, y_val)
    xgb_model.save_model(os.path.join(config.OUTPUT_DIR, 'model_xgb.json'))

    # ── LightGBM ──────────────────────────────────────────────────────────────
    log.info("─" * 60)
    lgb_model = train_lightgbm(X_train, y_train, X_val, y_val)
    with open(os.path.join(config.OUTPUT_DIR, 'model_lgb.pkl'), 'wb') as f:
        pickle.dump(lgb_model, f)

    # ── Platt calibration ─────────────────────────────────────────────────────
    log.info("─" * 60)
    log.info("Calibrating on val set ...")
    xgb_cal = calibrate_model(xgb_model, X_val, y_val)
    lgb_cal = calibrate_model(lgb_model, X_val, y_val)
    for fname, obj in [('model_xgb_cal.pkl', xgb_cal), ('model_lgb_cal.pkl', lgb_cal)]:
        with open(os.path.join(config.OUTPUT_DIR, fname), 'wb') as f:
            pickle.dump(obj, f)
    log.info("  Calibrated models saved.")

    # ── Scores ────────────────────────────────────────────────────────────────
    xgb_val_s      = xgb_model.predict_proba(X_val)[:, 1]
    lgb_val_s      = lgb_model.predict_proba(X_val)[:, 1]
    xgb_test_s     = xgb_model.predict_proba(X_test)[:, 1]
    xgb_cal_test_s = xgb_cal.predict_proba(X_test)[:, 1]
    lgb_test_s     = lgb_model.predict_proba(X_test)[:, 1]
    lgb_cal_test_s = lgb_cal.predict_proba(X_test)[:, 1]

    # ── Evaluation summary ────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("  EVALUATION — VAL SET")
    log.info("=" * 60)
    val_metrics = {}
    for name, scores in [('XGBoost', xgb_val_s), ('LightGBM', lgb_val_s)]:
        m = eval_model(name, scores, y_val, 'val')
        val_metrics[name] = m

    log.info("=" * 60)
    log.info("  EVALUATION — TEST SET")
    log.info("=" * 60)
    test_metrics = {}
    for name, scores in [
        ('Baseline (majority class)', b_test),
        ('XGBoost',                   xgb_test_s),
        ('XGBoost+Platt',             xgb_cal_test_s),
        ('LightGBM',                  lgb_test_s),
        ('LightGBM+Platt',            lgb_cal_test_s),
    ]:
        m = eval_model(name, scores, y_test, 'test')
        test_metrics[name] = m
    all_metrics = {'val': val_metrics, 'test': test_metrics}

    # ── Segment analysis: first-timers vs returning ───────────────────────────
    log.info("=" * 60)
    log.info("  SEGMENT ANALYSIS (XGBoost+Platt, TEST SET)")
    log.info("=" * 60)
    eval_by_segment('XGBoost+Platt', xgb_cal_test_s, y_test, first_order_test, 'test')

    # ── Feature importance ────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("  FEATURE IMPORTANCE")
    log.info("=" * 60)
    log_feature_importance(xgb_model, config.ALL_FEATURES, 'XGBoost')
    log_feature_importance(lgb_model, config.ALL_FEATURES, 'LightGBM')

    # ── Save metrics ──────────────────────────────────────────────────────────
    metrics_path = os.path.join(config.OUTPUT_DIR, 'metrics.json')
    with open(metrics_path, 'w') as f:
        json.dump(all_metrics, f, indent=2)
    log.info("Metrics saved → %s", metrics_path)

    # ── Plots ─────────────────────────────────────────────────────────────────
    save_roc_pr_curves(
        curves_data={
            'Baseline':       b_test,
            'XGBoost':        xgb_test_s,
            'XGBoost+Platt':  xgb_cal_test_s,
            'LightGBM':       lgb_test_s,
            'LightGBM+Platt': lgb_cal_test_s,
        },
        title_suffix='Discount Propensity (TEST SET)',
        filename='curves_test.png',
        y_true=y_test,
    )

    log.info("=" * 60)
    log.info("Done.")


if __name__ == '__main__':
    main()
