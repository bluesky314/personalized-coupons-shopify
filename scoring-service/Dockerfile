# Build context must be Discount/ (two levels up).
# Build with: docker build -t scoring-service -f shopify_app/scoring-service/Dockerfile .
#   (run from the Discount/ directory)

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY shopify_app/scoring-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy model source files (single source of truth -- no rewrite)
COPY model_handler.py        /app/model/model_handler.py
COPY config.py               /app/model/config.py
COPY 02_train.py             /app/model/02_train.py
COPY 09_user_level_model.py  /app/model/09_user_level_model.py

# Copy model artifacts
COPY outputs/model_user_level_lgb_cal.pkl  /app/model/outputs/
COPY outputs/model_lgb_cal.pkl             /app/model/outputs/
COPY outputs/preprocessor_stats.json       /app/model/outputs/
COPY outputs/tier_calibration.json         /app/model/outputs/

# Copy service code
COPY shopify_app/scoring-service/app.py /app/app.py

ENV MODEL_DIR=/app/model
ENV OUTPUTS_DIR=/app/model/outputs

EXPOSE 8081

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
