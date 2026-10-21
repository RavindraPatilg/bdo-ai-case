FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY pytest.ini .
# analyses.db is created at runtime and should live on a mounted volume in production
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
# Bind to the PORT provided by the platform (e.g. Render sets PORT),
# falling back to 8000 for local runs. One image works everywhere.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
