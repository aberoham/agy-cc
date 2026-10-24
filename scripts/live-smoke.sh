#!/usr/bin/env bash
# Live smoke test: runs every agy plugin command against the real agy and a
# real Gemini licence, in a throwaway repository, and checks the results.
#
# It uses your agy sign-in and your seat's quota, so it is a dry run unless
# you pass --apply. Run it on your own machine before a release; CI uses the
# fake agy in tests/ instead.
#
#   scripts/live-smoke.sh                     # print what would run
#   scripts/live-smoke.sh --apply             # run it (model: gemini-4-argon-low)
#   scripts/live-smoke.sh --apply --with-claude   # also drive two commands through claude --plugin-dir
#
# Environment: AGY_SMOKE_MODEL (default low), AGY_BIN (agy binary), CLAUDE_BIN.
set -euo pipefail

apply=false
with_claude=false
for arg in "$@"; do
    case "${arg}" in
        --apply) apply=true ;;
        --with-claude) with_claude=true ;;
        -h | --help)
            sed -n '2,14p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: ${arg}" >&2
            exit 2
            ;;
    esac
done

here="$(cd "$(dirname "$0")/.." && pwd)"
plugin="${here}/plugins/agy"
companion_py="${plugin}/scripts/agy_companion.py"
model="${AGY_SMOKE_MODEL:-low}"
python="${PYTHON:-python3}"

steps=(
    "setup: agy found, signed in, project set"
    "review: finds the planted bug; checkout unchanged; working copy removed"
    "adversarial-review: returns a verdict"
    "plan: returns a plan read from agy's artifact file"
    "task: edits only its working copy; queued commands are listed"
    "resume: sends command output back and finishes (only if commands were queued)"
    "apply --check: the task's patch applies to the checkout"
    "cancel: a background job stops and leaves no agy process"
)
if ! ${apply}; then
    echo "Dry run. With --apply this runs, on model ${model}:"
    printf '  - %s\n' "${steps[@]}"
    ${with_claude} && echo "  - claude --plugin-dir: /agy:setup and /agy:review --wait"
    echo "Each step calls the real agy and uses your Gemini licence."
    exit 0
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/agy-smoke.XXXXXX")"
export AGY_COMPANION_STATE_DIR="${work}/state"
repo="${work}/repo"
report="${work}/report.txt"
failures=0

log() { printf '%s\n' "$*" | tee -a "${report}"; }
pass() { log "PASS  $1 ($2 s)"; }
fail() {
    log "FAIL  $1: $2"
    failures=$((failures + 1))
}
companion() { (cd "${repo}" && "${python}" "${companion_py}" "$@"); }
newest_job() {
    "${python}" - "${AGY_COMPANION_STATE_DIR}" "$1" <<'EOF'
import json, sys
from pathlib import Path
jobs = [json.loads(p.read_text()) for p in Path(sys.argv[1]).glob("*/jobs/*/job.json")]
jobs = [j for j in jobs if j["kind"] == sys.argv[2]]
jobs.sort(key=lambda j: j["created_at"])
print(json.dumps(jobs[-1] if jobs else {}))
EOF
}
field() { "${python}" -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }

mkdir -p "${repo}"
git -C "${repo}" init -q -b main
printf 'def average(values):\n    return sum(values) / len(values)\n' >"${repo}/stats.py"
git -C "${repo}" add stats.py
git -C "${repo}" -c user.name=smoke -c user.email=smoke@localhost commit -qm init
printf 'def average(values):\n    total = 0\n    for i in range(1, len(values)):\n        total += values[i]\n    return total / len(values)\n' >"${repo}/stats.py"
checkout_state() { git -C "${repo}" status --porcelain; git -C "${repo}" diff; }
before="$(checkout_state)"

log "agy plugin live smoke test, $(date -u +%Y-%m-%dT%H:%M:%SZ), model ${model}"
log "Work directory: ${work}"

# setup
start=${SECONDS}
if out="$(companion setup --json)" && [[ "$(field "${out}" ready)" == "True" ]]; then
    pass setup $((SECONDS - start))
    log "      agy $(field "${out}" agy_version) at $(field "${out}" agy_path)"
else
    fail setup "not ready: ${out:-no output}"
    log "Stopping: agy must be ready for the other steps."
    exit 1
fi

# review
start=${SECONDS}
out="$(companion review --model "${model}" --wait-seconds 280 || true)"
job="$(newest_job review)"
if [[ "${out}" == *"Verdict:"* && "${out}" == *"stats.py"* ]] \
    && [[ "$(checkout_state)" == "${before}" ]] \
    && [[ ! -e "$(field "${job}" worktree)" ]]; then
    pass review $((SECONDS - start))
else
    fail review "$(field "${job}" status): ${out:0:300}"
fi

# adversarial review
start=${SECONDS}
out="$(companion adversarial-review --model "${model}" --wait-seconds 280 empty input || true)"
if [[ "${out}" == *"Verdict:"* ]]; then
    pass adversarial-review $((SECONDS - start))
else
    fail adversarial-review "${out:0:300}"
fi

# plan
start=${SECONDS}
out="$(companion plan --model "${model}" --wait-seconds 280 add a median function to stats.py || true)"
job="$(newest_job plan)"
if [[ "${out}" == *"Plan file:"* ]] && [[ "$(checkout_state)" == "${before}" ]]; then
    pass plan $((SECONDS - start))
else
    fail plan "$(field "${job}" status): ${out:0:300}"
fi

# task
start=${SECONDS}
out="$(companion task --model "${model}" --wait-seconds 280 "Fix the off-by-one bug in average() in stats.py. Then run: python3 -c 'import stats; print(stats.average([1, 2, 3]))'" || true)"
job="$(newest_job task)"
task_id="$(field "${job}" id)"
status="$(field "${job}" status)"
worktree="$(field "${job}" worktree)"
if [[ "${status}" == "completed" || "${status}" == "needs-approval" ]] \
    && [[ "$(checkout_state)" == "${before}" ]] \
    && [[ -d "${worktree}" ]]; then
    pass "task (${status})" $((SECONDS - start))
else
    fail task "${status}: ${out:0:300}"
fi

# resume, when agy queued commands. The smoke test runs the queued command
# itself only when it is the exact command the task asked for.
if [[ "${status}" == "needs-approval" ]]; then
    start=${SECONDS}
    results="${work}/results.txt"
    expected="python3 -c 'import stats; print(stats.average([1, 2, 3]))'"
    queued="$(field "${job}" queued_commands)"
    if [[ "${queued}" == *"import stats"* ]]; then
        rc=0
        output="$(cd "${worktree}" && python3 -c 'import stats; print(stats.average([1, 2, 3]))' 2>&1)" || rc=$?
        printf '$ %s\n%s\nexit status: %s\n' "${expected}" "${output}" "${rc}" >"${results}"
    else
        echo "The smoke test did not run: ${queued}" >"${results}"
    fi
    out="$(companion resume "${task_id}" "${results}" --wait-seconds 280 || true)"
    status="$(field "$(newest_job task)" status)"
    if [[ "${status}" == "completed" || "${status}" == "needs-approval" ]]; then
        pass "resume (${status})" $((SECONDS - start))
    else
        fail resume "${status}: ${out:0:300}"
    fi
else
    log "SKIP  resume: the task queued no commands"
fi

# apply --check
start=${SECONDS}
if out="$(companion apply --check "${task_id}" 2>&1)" && [[ "${out}" == *"apply cleanly"* || "${out}" == *"changed no files"* ]]; then
    pass "apply --check" $((SECONDS - start))
else
    fail "apply --check" "${out:0:300}"
fi

# cancel
start=${SECONDS}
companion plan --background --model "${model}" design a plugin system for stats.py >/dev/null
for _ in $(seq 1 30); do
    job="$(newest_job plan)"
    [[ -n "$(field "${job}" agy_pid)" ]] && break
    sleep 1
done
agy_pid="$(field "${job}" agy_pid)"
companion cancel "$(field "${job}" id)" >/dev/null || true
sleep 2
job="$(newest_job plan)"
if [[ "$(field "${job}" status)" == "cancelled" ]] && { [[ -z "${agy_pid}" ]] || ! kill -0 "${agy_pid}" 2>/dev/null; }; then
    pass cancel $((SECONDS - start))
else
    fail cancel "status $(field "${job}" status), agy pid ${agy_pid:-none}"
fi

# Claude Code, optional
if ${with_claude}; then
    claude_bin="${CLAUDE_BIN:-$(command -v claude || true)}"
    if [[ -z "${claude_bin}" ]]; then
        fail claude "claude not found; set CLAUDE_BIN"
    else
        for command in "/agy:setup" "/agy:review --wait --model ${model}"; do
            start=${SECONDS}
            if out="$(cd "${repo}" && "${claude_bin}" -p "${command}" --plugin-dir "${plugin}" </dev/null 2>&1)" && [[ -n "${out}" ]]; then
                pass "claude ${command}" $((SECONDS - start))
            else
                fail "claude ${command}" "${out:0:300}"
            fi
        done
    fi
fi

log ""
log "Approval rounds for the task job:"
(cd "${repo}" && "${python}" "${companion_py}" status "${task_id}" | sed -n '/^| Round/,/^$/p' | tee -a "${report}")
log "Report: ${report}"
if ((failures > 0)); then
    log "${failures} step(s) failed."
    exit 1
fi
log "All steps passed."
