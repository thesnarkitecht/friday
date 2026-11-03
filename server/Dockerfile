# The always-on FRIDAY hub: the same install as every device, running `friday serve`.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && rm -rf build
# FRIDAY_URL: `docker compose exec friday friday ask …` talks to this hub instead of starting a second FRIDAY.
ENV FRIDAY_HOME=/data FRIDAY_PIPER_VOICE=/opt/voices/en_GB-jenny_dioco-medium.onnx FRIDAY_MODELS_DIR=/opt/models \
    FRIDAY_URL=http://127.0.0.1:8080
# Ears (SenseVoice) and voice (Piper) baked in, so the container needs no download at runtime.
RUN python -c "from friday.voice import Voice; import sys; sys.exit(0 if Voice('/opt/voices/en_GB-jenny_dioco-medium.onnx', '/opt/models').ensure('en_GB-jenny_dioco-medium') else 1)"
EXPOSE 8080
CMD ["friday", "serve", "--host", "0.0.0.0", "--port", "8080"]
