FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 U2NET_HOME=/models DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the segmentation model into the image so the first conversion doesn't wait for a 180 MB download.
RUN python -c "from rembg import new_session; new_session('isnet-general-use')"
COPY app ./app
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
