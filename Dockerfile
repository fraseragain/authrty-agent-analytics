FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV CCFR_DATA_DIR=/app/data
ENV CCFR_DB_PATH=/app/data/ccfr.sqlite3
ENV CCFR_IMPORT_ROOT=/imports
ENV PYTHONPATH=/app/backend/src
COPY backend ./backend
# Uninstall the build-time pip/setuptools/wheel toolchain afterwards: the app
# doesn't need it at runtime, and setuptools vendors its own copies of
# jaraco.context and wheel that trail the base image's CVE fixes.
RUN pip install --no-cache-dir ./backend && \
    python -m pip uninstall --yes pip setuptools wheel
# Data assets live at the repo root; the image mirrors the checkout layout, so
# the defaults resolve to /app/pricing.csv and /app/demo/claude-export ("Load
# demo data" / `serve --demo`). Copied after the pip install so data edits
# don't invalidate the dependency layer. The optional dated-snapshot dir is
# not baked in: compose mounts ./pricing:/app/pricing at runtime.
COPY pricing.csv ./pricing.csv
COPY demo/claude-export ./demo/claude-export
EXPOSE 8000
CMD ["uvicorn", "ccfr.main:app", "--host", "0.0.0.0", "--port", "8000"]
