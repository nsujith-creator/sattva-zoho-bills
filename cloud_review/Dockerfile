FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY cloud_review/app.py cloud_review/review.py cloud_review/extraction.py cloud_review/auth.py cloud_review/container_start.py cloud_review/automation.py cloud_review/owner_start.py cloud_review/leakage.py cloud_review/leakage_app.py /app/
COPY cloud_review/static/sattva-swirl-white.svg /app/static/
COPY cloud_review/static/fonts/poppins-latin-400-normal.woff2 cloud_review/static/fonts/poppins-latin-500-normal.woff2 cloud_review/static/fonts/poppins-latin-600-normal.woff2 cloud_review/static/fonts/poppins-latin-700-normal.woff2 cloud_review/static/fonts/dm-mono-latin-400-normal.woff2 cloud_review/static/fonts/dm-mono-latin-500-normal.woff2 /app/static/fonts/
COPY src/zoho_bills.py /app/zoho_core.py
ENV PYTHONUNBUFFERED=1 BIND=0.0.0.0
USER 0:0
CMD ["python", "/app/container_start.py"]
