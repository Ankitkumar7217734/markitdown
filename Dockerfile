FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-hin \
        tesseract-ocr-eng \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py hindi_pdf.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 10000

# Render sets PORT. Timeout covers a long Hindi book plus OCR fallback.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-10000} --timeout 180 --workers 1 --threads 1 app:app"]
