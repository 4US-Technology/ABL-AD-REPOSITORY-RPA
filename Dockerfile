FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --uid 1000 --no-create-home --shell /bin/false rpauser \
    && chown -R rpauser:rpauser /app

USER rpauser

ENTRYPOINT ["python", "main.py"]
