# shellcheck shell=bash
# Sourced by build.sh and signin.sh, not run. Points docker at Colima when a
# Colima context exists and DOCKER_HOST is unset, then makes sure the daemon
# answers: a stopped Colima is started (the profile is read from the socket
# path, ~/.colima/<profile>/docker.sock), and anything else is an error.

if [[ -z "${DOCKER_HOST:-}" ]] && docker context inspect colima >/dev/null 2>&1; then
    DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' colima)"
    export DOCKER_HOST
fi

if ! docker info >/dev/null 2>&1; then
    colima_profile=""
    if [[ "${DOCKER_HOST:-}" =~ /\.colima/([^/]+)/docker\.sock$ ]]; then
        colima_profile="${BASH_REMATCH[1]}"
    fi
    if [[ -n "${colima_profile}" ]] && command -v colima >/dev/null 2>&1; then
        echo "Colima (profile ${colima_profile}) is not running; starting it."
        colima start --profile "${colima_profile}"
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "The Docker daemon at ${DOCKER_HOST:-the default socket} is not reachable." >&2
        echo "Start Colima or Docker Desktop, then run ${0} again." >&2
        exit 1
    fi
fi
