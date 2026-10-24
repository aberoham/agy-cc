#!/usr/bin/env bash
# Sign agy in for container jobs. The sign-in is interactive, so run this
# from a real terminal. agy's OAuth token is written to the container home
# (~/.gemini/agy-container-home, the directory `/agy:setup` names), not to
# the host's own ~/.gemini/antigravity-cli.
#
# Environment: AGY_IMAGE (default agy-sandbox:experiment), AGY_CONTAINER_HOME
# (another container home).
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
image="${AGY_IMAGE:-agy-sandbox:experiment}"
host_settings="${HOME}/.gemini/antigravity-cli/settings.json"

if [[ -z "${DOCKER_HOST:-}" ]] && docker context inspect colima >/dev/null 2>&1; then
    DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' colima)"
    export DOCKER_HOST
fi

home="$(/usr/bin/python3 - "${here}" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1] + "/plugins/agy/scripts")
from agylib import container
print(container.signin_home())
EOF
)"
settings_dir="${home}/.gemini/antigravity-cli"
mkdir -p "${settings_dir}"

# Copy only the Google Cloud project and location from the host settings, so
# the container uses the same Gemini Enterprise project without a hard-coded
# name. agy's own terminal sandbox needs user namespaces, which Docker's
# default seccomp profile blocks; the container is the boundary instead.
/usr/bin/python3 - "${host_settings}" "${settings_dir}/settings.json" <<'EOF'
import json, sys
try:
    host = json.load(open(sys.argv[1]))
except OSError:
    host = {}
out = {"enableTerminalSandbox": False}
if "gcp" in host:
    out["gcp"] = host["gcp"]
with open(sys.argv[2], "w") as handle:
    json.dump(out, handle, indent=2)
    handle.write("\n")
EOF

echo "Container home: ${home}"
echo "Choose the business sign-in and the Google Cloud project option."
echo "Workforce provider, if your organisation uses one: ${AGY_WIF_PROVIDER:-locations/global/workforcePools/POOL_ID/providers/PROVIDER_ID}"
echo "After the agy prompt appears, type /quit."
docker run --rm -it --user "$(id -u):$(id -g)" -e "HOME=${home}" -v "${home}:${home}" "${image}"
