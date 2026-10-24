# Experimental image that runs agy against a bind-mounted git worktree.
# Build context: this directory. certs/ holds the TLS interception roots
# (git-ignored); copy them from ~/.docker/certs.d/ before building.
FROM debian:bookworm-slim

COPY certs/ /usr/local/share/ca-certificates/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

ARG UID=501
ARG GID=20
RUN (getent group "${GID}" || groupadd -g "${GID}" agent) \
    && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash agent

# Google's installer checks the SHA-512 from its manifest. Assert the binary
# exists in the same layer, because a failed download can still exit 0.
RUN curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/install.sh \
    && bash /tmp/install.sh --dir /opt/agy/bin \
    && test -x /opt/agy/bin/agy \
    && rm /tmp/install.sh
ENV PATH=/opt/agy/bin:${PATH}

USER agent
# A named volume mounted here takes this directory's owner, so agy can write.
RUN mkdir -p /home/agent/.gemini
WORKDIR /workspace
ENTRYPOINT ["agy"]
