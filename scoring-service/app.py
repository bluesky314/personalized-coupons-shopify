"""
Scoring microservice — thin HTTP wrapper around model_handler.predict_full().
Runs on port 8081. Called by the Shopify web app.

Usage:
    uvicorn app:app --host 0.0.0.0 --port 8081
"""

import os
import sys
import time
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

# ── Path setup: make model_handler importable ────────────────────────────────
# In Docker, model_handler.py / config.py / 02_train.py / 09_user_level_model.py
# are all copied to /app/model/. Locally, resolve relative to this file.
_MODEL_DIR = os.environ.get(
    "MODEL_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "")),
)
sys.path.insert(0, _MODEL_DIR)

from model_handler import CouponPredictor, predict_full  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scoring-service")

# ── Load model at startup ────────────────────────────────────────────────────
OUTPUTS_DIR = os.environ.get(
    "OUTPUTS_DIR",
    os.path.join(_MODEL_DIR, "outputs"),
)
predictor = CouponPredictor(OUTPUTS_DIR)
predictor.load()

# ── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(title="Coupon Scoring Service", version="1.0.0")


class ScoreRequest(BaseModel):
    user_id: Optional[str] = "shopify_user"
    order_id: Optional[str] = None

    # Order / cart context
    order_total: float = 0.0
    mrp_total: float = 0.0
    cart_item_count: int = 1

    # User history
    total_prior_orders: int = 0
    prior_coupon_use_rate: Optional[float] = None
    eligible_coupon_use_rate: Optional[float] = None
    prior_eligible_orders: Optional[int] = None
    avg_coupon_value_used: Optional[float] = None
    average_order_value: Optional[float] = None
    gross_lifetime_value: Optional[float] = None
    days_since_last_order: Optional[float] = None
    days_since_first_order: Optional[float] = None
    prior_cod_rate: Optional[float] = None
    is_first_order: int = 0

    # Category / behavioral
    top_category: Optional[str] = None
    category_concentration: Optional[float] = None
    max_order_value: Optional[float] = None
    avg_order_gap_days: Optional[float] = None

    # Company / channel
    company_id: Optional[int] = None
    sales_channel_id: Optional[int] = None

    # Geo
    delivery_pincode: Optional[str] = None
    delivery_city: Optional[str] = None
    delivery_state: Optional[str] = None


@app.post("/score")
def score(req: ScoreRequest):
    """Run predict_full() and return tier + coupon recommendation + strategies."""
    if predictor.user_model is None:
        raise HTTPException(503, "Model not loaded")
    t0 = time.time()
    try:
        result = predict_full(req.model_dump(), predictor)
    except Exception as e:
        log.exception("Scoring failed")
        raise HTTPException(500, str(e))
    elapsed_ms = round((time.time() - t0) * 1000, 1)
    log.info("Scored user=%s tier=%s in %.1fms", req.user_id, result.get("tier"), elapsed_ms)
    return result


@app.get("/health")
def health():
    return {
        "status": "ok" if predictor.user_model is not None else "degraded",
        "models_loaded": predictor.models_loaded,
    }
