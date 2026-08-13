FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY reference_transfer ./reference_transfer

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data

CMD ["reference-transfer", "work"]
