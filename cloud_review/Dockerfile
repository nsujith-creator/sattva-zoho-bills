FROM python:3.12-slim
WORKDIR /app
COPY app.py review.py extraction.py auth.py container_start.py /app/
ENV PYTHONUNBUFFERED=1 BIND=0.0.0.0
USER 0:0
CMD ["python", "/app/container_start.py"]
