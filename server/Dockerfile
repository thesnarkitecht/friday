FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-voice.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-voice.txt
# Speech runs here (Whisper in, Piper out) so the devices with headphones stay light. Voice baked in.
RUN mkdir -p /opt/voices && python -m piper.download_voices --data-dir /opt/voices en_US-lessac-medium
COPY friday ./friday
COPY pyproject.toml ./
RUN pip install --no-cache-dir --no-deps . && rm -rf build  # the `friday` CLI inside the container
COPY plugins ./plugins
COPY skills ./skills.seed
COPY start.sh ./
ENV FRIDAY_DB=/data/friday.db FRIDAY_SKILLS_DIR=/data/skills FRIDAY_PLUGINS_DIR=/app/plugins \
    FRIDAY_PIPER_VOICE=/opt/voices/en_US-lessac-medium.onnx HF_HOME=/data/hf
EXPOSE 8080
CMD ["./start.sh"]
