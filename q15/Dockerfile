# Optional: deploy to Render with the Docker runtime instead of the native
# Python runtime. Render sets $PORT; uvicorn binds to it.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT=10000
EXPOSE 10000

# Warm the dataset once at startup so the first graded question is already fast.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
