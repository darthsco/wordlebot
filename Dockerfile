FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WORDLEBOT_CONFIG=/app/config.toml \
    WORDLEBOT_DATABASE_PATH=/app/state/wordlebot.sqlite3

WORKDIR /app

RUN useradd --create-home --uid 10001 wordlebot \
    && mkdir -p /app/state \
    && chown wordlebot:wordlebot /app/state

# Dependencies first, from exact pins, so this layer is rebuilt only when
# requirements.txt changes and a rebuild never resolves new releases.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# --no-deps: everything was installed above; fail the build if the pins no
# longer satisfy pyproject.toml rather than letting pip resolve around them.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps . && pip check

COPY data ./data

USER wordlebot
VOLUME ["/app/state"]

# Python is PID 1 and installs no SIGTERM handler, so the kernel would drop
# Docker's default stop signal and the container would be SIGKILLed after the
# grace period. SIGINT raises KeyboardInterrupt, which shuts the bot down cleanly.
STOPSIGNAL SIGINT
ENTRYPOINT ["python", "-m", "wordlebot"]
CMD ["run"]
