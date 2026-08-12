# atp — Autonomous Multi-Asset Trading desk (§21 deployment)
#
# Build:  docker build -t atp .
# Run  :  docker run --rm atp backtest --bars 400      # config-driven paper backtest, no creds
#
# Live/IBKR needs a reachable IB Gateway (run on the host or a dedicated ib-gateway container)
# and market-data subscriptions — see docs/IBKR_SETUP.md.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATP_LOG_LEVEL=INFO

WORKDIR /app

# Install the package. Build arg EXTRAS lets you add live/ml deps:
#   docker build --build-arg EXTRAS=".[live,ml]" -t atp .
ARG EXTRAS="."
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e "${EXTRAS}"

# Non-root runtime user.
RUN useradd -m atp && chown -R atp /app
USER atp

ENTRYPOINT ["python", "-m", "atp"]
CMD ["version"]
