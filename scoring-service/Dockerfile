FROM python:3.11-slim

# LightGBM requires libgomp
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Model source files
COPY model_files/model_handler.py /app/model/model_handler.py
COPY model_files/config.py        /app/model/config.py
COPY model_files/02_train.py      /app/model/02_train.py
COPY model_files/09_user_level_model.py /app/model/09_user_level_model.py

# Model artifacts
COPY model_files/outputs/ /app/model/outputs/

# Service
COPY app.py /app/app.py

ENV MODEL_DIR=/app/model
ENV OUTPUTS_DIR=/app/model/outputs

EXPOSE 8081

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
