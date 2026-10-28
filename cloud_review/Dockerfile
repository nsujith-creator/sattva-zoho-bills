FROM python:3.12-slim
WORKDIR /app
COPY cloud_review/app.py cloud_review/review.py cloud_review/extraction.py cloud_review/auth.py cloud_review/container_start.py cloud_review/automation.py /app/
COPY src/zoho_bills.py /app/zoho_core.py
ENV PYTHONUNBUFFERED=1 BIND=0.0.0.0
USER 0:0
CMD ["python", "/app/container_start.py"]
