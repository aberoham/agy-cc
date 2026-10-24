#!/usr/bin/env bash
# Sign agy in inside the experimental container. The OAuth token is written
# to the named volume agy-home, never to the host. Run from a real terminal:
# the sign-in is interactive.
set -euo pipefail

image="${AGY_IMAGE:-agy-sandbox:experiment}"
volume="${AGY_VOLUME:-agy-home}"
host_settings="${HOME}/.gemini/antigravity-cli/settings.json"

if [[ -z "${DOCKER_HOST:-}" ]] && docker context inspect colima >/dev/null 2>&1; then
    DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' colima)"
    export DOCKER_HOST
fi

# Copy only the Google Cloud project and location from the host settings, so
# the container uses the same Gemini Enterprise project without a hard-coded
# name. The terminal sandbox needs user namespaces, which Docker blocks.
settings="$(/usr/bin/python3 - "${host_settings}" <<'EOF'
import json, sys
try:
    host = json.load(open(sys.argv[1]))
except OSError:
    host = {}
out = {"enableTerminalSandbox": False}
if "gcp" in host:
    out["gcp"] = host["gcp"]
print(json.dumps(out))
EOF
)"

docker volume create "${volume}" >/dev/null
docker run --rm -i --entrypoint /bin/sh -v "${volume}:/home/agent/.gemini" "${image}" \
    -c 'mkdir -p ~/.gemini/antigravity-cli && cat > ~/.gemini/antigravity-cli/settings.json' \
    <<<"${settings}"

echo "Choose the business sign-in and the Google Cloud project option."
echo "Workforce provider, if your organisation uses one: ${AGY_WIF_PROVIDER:-locations/global/workforcePools/POOL_ID/providers/PROVIDER_ID}"
echo "After the agy prompt appears, type /quit."
docker run --rm -it -v "${volume}:/home/agent/.gemini" "${image}"
