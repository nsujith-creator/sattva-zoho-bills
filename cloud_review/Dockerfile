FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/app.py
ENV PYTHONUNBUFFERED=1 BIND=0.0.0.0
USER 65532:65532
CMD ["python", "/app/app.py"]