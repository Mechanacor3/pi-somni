# Test-only image: pinned Pi plus python3/git for the plugin's stdlib backend.
#   docker build -t pi-plugin-e2e:pi-1.0.0 tests/e2e
FROM node:22-bookworm-slim
ARG PI_VERSION=1.0.0
RUN npm install --global --omit=dev "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force \
    && apt-get update && apt-get install --yes --no-install-recommends python3 git \
    && rm -rf /var/lib/apt/lists/*
ENV PI_OFFLINE=1
