FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --uid 1000 --no-create-home --shell /bin/false rpauser \
    && mkdir -p /data \
    && chown -R rpauser:rpauser /app /data

USER rpauser

ENTRYPOINT ["ad-rpa"]
