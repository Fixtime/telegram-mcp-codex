# Override with an organization-approved digest for immutable base-image provenance.
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm
FROM ${PYTHON_IMAGE} AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM base AS production
COPY requirements-analysis.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-analysis.lock
COPY telegram_analysis ./telegram_analysis
RUN groupadd -g 10001 analysis && useradd -u 10001 -g analysis -M analysis
USER 10001:10001
ENTRYPOINT ["python", "-m", "telegram_analysis.server"]
CMD ["--config", "/state/config.json"]

FROM base AS development
COPY requirements-analysis-dev.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-analysis-dev.lock
COPY telegram_analysis ./telegram_analysis
COPY analysis_tests ./analysis_tests
COPY main.py telegram_analysis_mcp.py ./
CMD ["python", "-m", "pytest", "analysis_tests", "-q"]
