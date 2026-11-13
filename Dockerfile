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

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY data ./data

USER wordlebot
VOLUME ["/app/state"]

ENTRYPOINT ["python", "-m", "wordlebot"]
CMD ["run"]
