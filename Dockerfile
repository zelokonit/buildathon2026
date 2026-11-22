# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# 1. Fetch sqlglot at the pinned commit
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/tobymao/sqlglot /opt/sqlglot \
    && cd /opt/sqlglot \
    && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && rm -rf .git

# Experiment: report a 1 s+ test only when at most 10 tests reached the changed statement.
ENV SEL_MAX_TEST_SECS=1

# 3. Copy your own code and any committed maps
COPY --chown=65534:65534 selector.py /app/selector.py
COPY maps/ /app/maps/

CMD ["python", "/app/selector.py"]
