# The image the plugin runs agy in. The companion mounts the job's working
# copy, one job subdirectory, the plugin files and the container home at their
# host paths and sets HOME to that container home. Build context: this
# directory. certs/ holds the TLS interception roots (git-ignored); copy them
# from ~/.docker/certs.d/ before building.
#
# Build with the host user's ids, so files the image creates and files on the
# bind mounts share an owner without any chown at start:
#   docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) -t agy-sandbox:experiment containers
# /agy:setup checks the image's user against yours before a job starts.
FROM debian:bookworm-slim

COPY certs/ /usr/local/share/ca-certificates/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq python3 \
    && update-ca-certificates \
    && apt-get clean
# uv for Python repositories: agy runs their tests with `uv run`. Its
# installer is a `curl | sh`, so the binary is asserted in the same layer.
# UV_SYSTEM_CERTS makes uv trust the interception roots above; the copy link
# mode avoids hard-link failures between the cache and a bind mount.
RUN curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh \
    && env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh \
    && test -x /usr/local/bin/uv \
    && rm /tmp/uv-install.sh
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    UV_SYSTEM_CERTS=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

# The group id may already exist in the base image (macOS staff is 20, which
# Debian gives to dialout); the agent user then simply shares it.
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

# Numeric, so `docker image inspect --format '{{.Config.User}}'` can be
# compared with the host user before a job starts.
USER ${UID}:${GID}
ENTRYPOINT ["agy"]
