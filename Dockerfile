# Triveni API image. Cold start, no credentials, no network at runtime.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    TRIVENI_RAILS=offline \
    TRIVENI_LLM_MODE=replay

WORKDIR /app

# Dependency layer first so source edits do not bust the wheel cache.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip && \
    mkdir -p core recon forecast qa api data ingest cli && \
    pip install --no-cache-dir -e .

COPY . .

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
