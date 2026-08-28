# GPU-capable image. Docker is used rather than the Gradio SDK because
# PaddlePaddle needs system libraries the default Space image does not carry.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# A Space runs as a non-root user; give it a writable home for the model cache.
RUN useradd -m -u 1000 app
ENV HOME=/home/app \
    PADDLE_PDX_CACHE_HOME=/home/app/.paddlex \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /home/app
COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app app.py .

USER app
EXPOSE 7860
CMD ["python", "app.py"]
ifUUGyfqPUfKkK