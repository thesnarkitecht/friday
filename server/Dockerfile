# The always-on FRIDAY hub: the same install as every device, running `friday serve`.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && rm -rf build
# FRIDAY_URL: `docker compose exec friday friday ask …` talks to this hub instead of starting a second FRIDAY.
ENV FRIDAY_HOME=/data FRIDAY_PIPER_VOICE=/opt/voices/en_GB-jenny_dioco-medium.onnx HF_HOME=/data/hf \
    FRIDAY_URL=http://127.0.0.1:8080
# Voice baked in so the container needs no download at runtime.
RUN python -m piper.download_voices --data-dir /opt/voices en_GB-jenny_dioco-medium
EXPOSE 8080
CMD ["friday", "serve", "--host", "0.0.0.0", "--port", "8080"]
