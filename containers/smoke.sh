#!/usr/bin/env bash
# Run one print-mode turn in a fresh container against a scratch git
# repository, after signin.sh. Shows whether the token in the volume works
# headless and whether a write lands on the host through the bind mount.
set -euo pipefail

image="${AGY_IMAGE:-agy-sandbox:experiment}"
volume="${AGY_VOLUME:-agy-home}"
model="${AGY_MODEL:-gemini-4-argon-low}"

if [[ -z "${DOCKER_HOST:-}" ]] && docker context inspect colima >/dev/null 2>&1; then
    DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' colima)"
    export DOCKER_HOST
fi

work="$(mktemp -d)"
git -C "${work}" init -q
echo original >"${work}/tracked.txt"

prompt='{"event":"user","message":{"role":"user","content":"Run the command pwd, then create a file container-probe.txt containing hi. Report each tool result."}}'
echo "${prompt}" | docker run --rm -i \
    --user "$(id -u):$(id -g)" \
    -v "${volume}:/home/agent/.gemini" -e HOME=/home/agent \
    -v "${work}:/workspace" \
    "${image}" --input-format stream-json --output-format stream-json \
    --model "${model}" --mode accept-edits --print= \
    | /usr/bin/python3 -c '
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    if e["event"] == "result":
        r = e["result"]
        print("status:", r["status"], r.get("error", ""))
        print(r.get("response", "")[:400])
'
echo "Host directory ${work}:"
ls -la "${work}"
