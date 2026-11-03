# The always-on FRIDAY hub: the same install as every device, running `friday serve`.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && rm -rf build
# FRIDAY_URL: `docker compose exec friday friday ask …` talks to this hub instead of starting a second FRIDAY.
ENV FRIDAY_HOME=/data FRIDAY_MODELS_DIR=/opt/models \
    FRIDAY_URL=http://127.0.0.1:8080
# Ears (SenseVoice) and voice (Piper) baked in, so the container needs no download at runtime.
RUN python -c "import sys; from friday.voice import Voice; sys.exit(0 if Voice(models_dir='/opt/models').ensure() else 1)"
EXPOSE 8080
CMD ["friday", "serve", "--host", "0.0.0.0", "--port", "8080"]
