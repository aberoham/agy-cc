#!/usr/bin/env bash
# Sign agy in for container jobs. The sign-in is interactive, so run this
# from a real terminal. agy's OAuth token is written to the container home
# (~/.gemini/agy-container-home, the directory `/agy:setup` names), not to
# the host's own ~/.gemini/antigravity-cli.
#
# Environment: AGY_IMAGE (default agy-sandbox:experiment), AGY_CONTAINER_HOME
# (another container home), AGY_WIF_PROVIDER (the workforce provider string;
# when unset, it is read from the agy wrapper on PATH).
set -euo pipefail

if [[ ! -t 0 ]]; then
    echo "The sign-in needs a terminal. Run ${0} in a terminal window, not through Claude Code's ! prefix." >&2
    exit 1
fi

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

# This script replaces the settings and token in the container home, so it
# must never be pointed at the host's own agy directory.
host_dir="${HOME}/.gemini/antigravity-cli"
if [[ -d "${host_dir}" && "$(cd "${settings_dir}" && pwd -P)" == "$(cd "${host_dir}" && pwd -P)" ]]; then
    echo "The container home ${home} is the host's own home; set AGY_CONTAINER_HOME to another directory." >&2
    exit 1
fi

# Remove the old sign-in so agy always shows the full flow, including the
# licence choice. With the old token in place agy starts signed in, even
# when Google has reclaimed the licence that sign-in chose.
rm -f "${settings_dir}/antigravity-oauth-token"

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

# The workforce provider string comes from AGY_WIF_PROVIDER, or else from a
# managed agy wrapper that copies it to the clipboard before a first sign-in.
# The wrapper may be a symbolic link, or come after the real binary on PATH.
provider="${AGY_WIF_PROVIDER:-}"
for wrapper in "$(command -v agy || true)" /usr/local/bin/agy; do
    [[ -z "${provider}" && -f "${wrapper}" ]] || continue
    if file -bL "${wrapper}" | grep -q text; then
        provider="$(grep -oE 'locations/global/workforcePools/[^/]+/providers/[A-Za-z0-9_-]+' "${wrapper}" | head -n 1 || true)"
    fi
done

echo "Container home: ${home}"
echo
echo "In agy (the menu labels differ between agy releases):"
if [[ -n "${provider}" ]]; then
    if command -v pbcopy >/dev/null 2>&1 && printf '%s' "${provider}" | pbcopy; then
        echo "  The provider string below is on the clipboard."
    fi
    echo "  Newer menu: Business account, then Advanced WIF Configuration."
    echo "  Older menu: Use a Google Cloud project, then Use advanced SSO config."
    echo "  Do not choose Google OAuth or Continue with Google Cloud."
    echo "  Paste the provider string when asked:"
    echo "    ${provider}"
    echo "  Sign in through your identity provider, then pick the Gemini Enterprise"
    echo "  licence and the project if asked."
else
    echo "  Newer menu: Business account, then Continue with Google Cloud."
    echo "  Older menu: Use a Google Cloud project."
    echo "  If your organisation uses workforce identity federation, choose Advanced"
    echo "  WIF Configuration (older: Use advanced SSO config) instead and enter"
    echo "  locations/global/workforcePools/POOL_ID/providers/PROVIDER_ID; set"
    echo "  AGY_WIF_PROVIDER to have this script supply it."
    echo "  Pick the Gemini Enterprise licence and the project if asked."
fi
echo "  When the agy prompt appears, type /quit."
echo
if ! read -r -p "Press Enter to start agy. " _; then
    echo >&2
    echo "Cancelled before agy started. The old container sign-in is already removed; run ${0} again to sign in." >&2
    exit 1
fi
docker run --rm -it --user "$(id -u):$(id -g)" -e "HOME=${home}" -v "${home}:${home}" "${image}"

# Prove the sign-in works the way a job uses it: copy only the token and
# settings into a fresh home, then list models in a new container. A missing
# or corrupted token gets "Please sign in" instead, a reclaimed licence gets
# 403 GEMINI_ENTERPRISE_LICENSE_NOT_VALID, and no prompt is sent. agy exits 0
# either way, so read the output. The plugin's default models are Gemini 4
# Argon, so the list must include them.
token="${settings_dir}/antigravity-oauth-token"
if [[ ! -s "${token}" ]]; then
    echo "Sign-in failed: agy wrote no token to ${settings_dir}. Run ${0} again." >&2
    exit 1
fi
signed_in="$(/usr/bin/python3 -c '
import json, sys
token = json.load(open(sys.argv[1]))
print(token.get("wif_provider") or "")
print("method {}, tier {}, project {}, region {}".format(
    token.get("auth_method"), token.get("user_tier"), token.get("project_id"), token.get("region")))
' "${token}")"
used_provider="$(head -n 1 <<<"${signed_in}")"
echo "agy saved a sign-in: $(tail -n 1 <<<"${signed_in}")"
if [[ -n "${provider}" && "${used_provider}" != "${provider}" ]]; then
    echo "Sign-in used the wrong method: expected workforce provider ${provider}, got '${used_provider}'." >&2
    echo "Run ${0} again and choose Advanced WIF Configuration (older: Use advanced SSO config)." >&2
    exit 1
fi

# The token copy is deleted outright, not moved to the Trash, so no copy of
# the credential survives; the rest of the temporary home goes to the Trash.
umask 077
mkdir -p "${HOME}/.cache"
check_home="$(mktemp -d "${HOME}/.cache/agy-signin-check.XXXXXX")"
check_token="${check_home}/.gemini/antigravity-cli/antigravity-oauth-token"
cleanup() {
    if ! rm -f "${check_token}"; then
        echo "Could not delete the token copy ${check_token}; delete it by hand." >&2
        exit 1
    fi
    trash "${check_home}" 2>/dev/null || echo "Could not move ${check_home} to the Trash; it holds no token." >&2
}
trap cleanup EXIT
mkdir -p "${check_home}/.gemini/antigravity-cli"
cp "${token}" "${settings_dir}/settings.json" "${check_home}/.gemini/antigravity-cli/"
echo "Checking the sign-in in a fresh container..."
reply="$(docker run --rm --init --user "$(id -u):$(id -g)" -e "HOME=${check_home}" -v "${check_home}:${check_home}" -w "${check_home}" \
    "${image}" models </dev/null 2>&1 || true)"
if [[ "${reply}" != *Error:* ]] && grep -q '^gemini-4-argon-' <<<"${reply}"; then
    echo "Sign-in works: a fresh container listed the Gemini 4 Argon models with the copied token."
else
    echo "Sign-in check failed. agy said:" >&2
    printf '%s\n' "${reply}" | tail -n 20 >&2
    exit 1
fi
