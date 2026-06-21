# Patched openBalena remote builder.
#
# The community image (obcommunity/open-balena-builder) bundles a balena CLI that
# HARD-EXPIRES (~365 days after the next CLI major), which silently breaks all builds.
# This image swaps in a current standalone CLI and wraps it so the builder's spawned
# CLI process always has HOME set and skips TLS verification for the self-signed backend
# (the builder spawns the CLI with a stripped env, so these MUST live in the wrapper).
FROM obcommunity/open-balena-builder:latest
ARG BALENA_CLI_VERSION=v22.1.2
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fSL -o /tmp/balena.tar.gz \
      "https://github.com/balena-io/balena-cli/releases/download/${BALENA_CLI_VERSION}/balena-cli-${BALENA_CLI_VERSION}-linux-x64-standalone.tar.gz"; \
    rm -f /usr/local/bin/balena; \
    rm -rf /usr/local/lib/balena-cli; \
    mkdir -p /usr/local/lib/balena-cli /root; \
    tar -xzf /tmp/balena.tar.gz -C /usr/local/lib/balena-cli --strip-components=1; \
    rm -f /tmp/balena.tar.gz; \
    test -x /usr/local/lib/balena-cli/bin/balena; \
    printf '#!/bin/sh\nexport HOME="${HOME:-/root}"\nexport NODE_TLS_REJECT_UNAUTHORIZED=0\nexec /usr/local/lib/balena-cli/bin/balena "$@"\n' > /usr/local/bin/balena; \
    chmod +x /usr/local/bin/balena; \
    env -i /usr/local/bin/balena version
