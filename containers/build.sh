#!/usr/bin/env bash
# Build the image the plugin runs agy in, for the current user's ids, then
# delete the untagged images earlier builds of this project left behind.
# Rebuilding does not touch the container sign-in, so after the image is
# deleted (a `docker system prune`, a new Colima disk) this is all that is
# needed.
#
# Environment: AGY_IMAGE (default agy-sandbox:experiment).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
image="${AGY_IMAGE:-agy-sandbox:experiment}"
# shellcheck source=containers/docker-up.sh
source "${here}/docker-up.sh"

# The TLS interception roots are git-ignored; take them from Docker's own
# certificate directory when this checkout has none.
shopt -s nullglob
local_certs=("${here}"/certs/*.crt)
docker_certs=("${HOME}"/.docker/certs.d/*.crt)
if ((${#local_certs[@]} == 0 && ${#docker_certs[@]} > 0)); then
    echo "Copying ${#docker_certs[@]} interception root(s) from ~/.docker/certs.d."
    cp "${docker_certs[@]}" "${here}/certs/"
fi

docker build --build-arg "UID=$(id -u)" --build-arg "GID=$(id -g)" -t "${image}" "${here}"
# Each rebuild leaves the previous image untagged. The label limits the prune
# to this project's images.
docker image prune --force --filter label=agy-cc.image >/dev/null
echo "Built ${image} for user $(id -u):$(id -g)."
