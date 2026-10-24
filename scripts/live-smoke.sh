#!/usr/bin/env bash
# Live smoke test: runs the agy plugin's companion commands against the real
# agy and a real Gemini licence, in a throwaway repository, and checks the
# results.
#
# It uses your agy sign-in and your seat's quota, so it is a dry run unless
# you pass --apply. Run it on your own machine before a release; CI uses the
# fake agy in tests/ instead.
#
#   scripts/live-smoke.sh                         # print what would run
#   scripts/live-smoke.sh --apply                 # run it (model: gemini-4-argon-low)
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
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: ${arg}" >&2
            exit 2
            ;;
    esac
done

# The script works only in the throwaway repository it creates; inherited
# variables that point git at another repository must not apply.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_CEILING_DIRECTORIES

here="$(cd "$(dirname "$0")/.." && pwd)"
plugin="${here}/plugins/agy"
companion_py="${plugin}/scripts/agy_companion.py"
model="${AGY_SMOKE_MODEL:-low}"
python="${PYTHON:-python3}"

steps=(
    "setup: agy found, signed in, project set"
    "review: names the planted off-by-one bug; checkout unchanged; working copy removed"
    "adversarial-review: returns a verdict"
    "plan: returns a plan read from agy's artifact file"
    "task (the engine behind /agy:rescue): edits only its working copy and changes stats.py"
    "resume: if agy queued exactly the requested test command, runs it under a write sandbox and resumes to completion"
    "result: shows the task's changed files"
    "check: the fixed average() returns 2.0, run under a write sandbox (macOS only)"
    "apply --check: the task's patch applies to the checkout"
    "cancel: a background job stops and leaves no agy process"
)
if ! ${apply}; then
    echo "Dry run. With --apply this runs, on model ${model}:"
    printf '  - %s\n' "${steps[@]}"
    if ${with_claude}; then
        echo "  - claude --plugin-dir: /agy:setup and /agy:review --wait"
    fi
    echo "Each step calls the real agy and uses your Gemini licence."
    exit 0
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/agy-smoke.XXXXXX")"
work="$(cd "${work}" && pwd -P)"
export AGY_COMPANION_STATE_DIR="${work}/state"
repo="${work}/repo"
report="${work}/report.txt"
failures=0
expected="python3 -c 'import stats; print(stats.average([1, 2, 3]))'"

log() { printf '%s\n' "$*" | tee -a "${report}"; }
pass() { log "PASS  $1 ($2 s)"; }
# A warning is a model choice the plugin handled correctly; it does not fail the run.
warn() { log "WARN  $1: $2"; }
fail() {
    log "FAIL  $1: $2"
    failures=$((failures + 1))
}
companion() { (cd "${repo}" && "${python}" "${companion_py}" "$@"); }
newest_job() {
    "${python}" - "${AGY_COMPANION_STATE_DIR}" "$1" <<'PY' || echo "{}"
import json, sys
from pathlib import Path
jobs = [json.loads(p.read_text()) for p in Path(sys.argv[1]).glob("*/jobs/*/job.json")]
jobs = [j for j in jobs if j["kind"] == sys.argv[2]]
jobs.sort(key=lambda j: j["created_at"])
print(json.dumps(jobs[-1] if jobs else {}))
PY
}
field() {
    "${python}" -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2" || true
}
queued_exactly() {
    "${python}" -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("queued_commands") == [sys.argv[2]] else 1)' "$1" "$2"
}
# Run a command whose code agy wrote, with writes limited to one directory.
# Only macOS has sandbox-exec; elsewhere the command is not run (exit 125).
run_confined() {
    local dir="$1"
    shift
    if [[ ! -x /usr/bin/sandbox-exec ]]; then
        echo "not run: no write sandbox on this platform"
        return 125
    fi
    local profile="(version 1)(allow default)(deny file-write*)(allow file-write* (subpath \"${dir}\") (literal \"/dev/null\"))"
    (cd "${dir}" && PYTHONDONTWRITEBYTECODE=1 /usr/bin/sandbox-exec -p "${profile}" "$@" 2>&1)
}

mkdir -p "${repo}"
git -C "${repo}" init -q -b main
printf 'def average(values):\n    return sum(values) / len(values)\n' >"${repo}/stats.py"
git -C "${repo}" add stats.py
git -C "${repo}" -c user.name=smoke -c user.email=smoke@localhost commit -qm init
printf 'def average(values):\n    total = 0\n    for i in range(1, len(values)):\n        total += values[i]\n    return total / len(values)\n' >"${repo}/stats.py"
checkout_state() {
    git -C "${repo}" status --porcelain
    git -C "${repo}" diff
}
before="$(checkout_state)"

log "agy plugin live smoke test, $(date -u +%Y-%m-%dT%H:%M:%SZ), model ${model}"
log "Work directory: ${work}"

# setup
start=${SECONDS}
out=""
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
rc=0
out="$(companion review --model "${model}" --wait-seconds 280)" || rc=$?
job="$(newest_job review)"
lower="$(printf '%s' "${out}" | tr '[:upper:]' '[:lower:]')"
if ((rc == 0)) && [[ "${out}" == *"Verdict: needs-attention"* ]] \
    && [[ "${lower}" == *"off-by-one"* || "${lower}" == *"off by one"* || "${lower}" == *"values[0]"* || "${lower}" == *"first element"* ]] \
    && [[ "$(checkout_state)" == "${before}" ]] \
    && [[ ! -e "$(field "${job}" worktree)" ]]; then
    pass review $((SECONDS - start))
else
    fail review "exit ${rc}, $(field "${job}" status): ${out:0:300}"
fi

# adversarial review
start=${SECONDS}
rc=0
out="$(companion adversarial-review --model "${model}" --wait-seconds 280 empty input)" || rc=$?
if ((rc == 0)) && [[ "${out}" == *"Verdict:"* ]] && [[ "$(checkout_state)" == "${before}" ]]; then
    pass adversarial-review $((SECONDS - start))
else
    fail adversarial-review "exit ${rc}: ${out:0:300}"
fi

# plan
start=${SECONDS}
rc=0
out="$(companion plan --model "${model}" --wait-seconds 280 add a median function to stats.py)" || rc=$?
job="$(newest_job plan)"
if [[ "${out}" == *"Plan file:"* ]] && [[ "$(checkout_state)" == "${before}" ]]; then
    pass "plan ($(field "${job}" status))" $((SECONDS - start))
elif ((rc == 0)) && [[ "$(field "${job}" status)" == "needs-approval" ]] && [[ "$(checkout_state)" == "${before}" ]]; then
    warn plan "agy stopped to ask for commands before writing a plan: $(field "${job}" queued_commands)"
else
    fail plan "exit ${rc}, $(field "${job}" status): ${out:0:300}"
fi

# task
start=${SECONDS}
rc=0
out="$(companion task --model "${model}" --wait-seconds 280 "Fix the off-by-one bug in average() in stats.py. Then run exactly: ${expected}")" || rc=$?
job="$(newest_job task)"
task_id="$(field "${job}" id)"
status="$(field "${job}" status)"
worktree="$(field "${job}" worktree)"
if [[ "${status}" == "completed" || "${status}" == "needs-approval" ]] \
    && [[ "$(checkout_state)" == "${before}" ]] \
    && [[ -n "${worktree}" && -d "${worktree}" ]] \
    && ! cmp -s "${repo}/stats.py" "${worktree}/stats.py"; then
    pass "task (${status})" $((SECONDS - start))
else
    fail task "exit ${rc}, ${status}: ${out:0:300}"
fi

# resume, only when agy queued exactly the command the task asked for
if [[ "${status}" == "needs-approval" ]]; then
    start=${SECONDS}
    if queued_exactly "${job}" "${expected}"; then
        results="${work}/results.txt"
        crc=0
        output="$(run_confined "${worktree}" python3 -c 'import stats; print(stats.average([1, 2, 3]))')" || crc=$?
        printf '$ %s\n%s\nexit status: %s\n' "${expected}" "${output}" "${crc}" >"${results}"
        rc=0
        out="$(companion resume "${task_id}" "${results}" --wait-seconds 280)" || rc=$?
        status="$(field "$(newest_job task)" status)"
        if ((crc == 0)) && ((rc == 0)) && [[ "${status}" == "completed" ]]; then
            pass resume $((SECONDS - start))
        else
            fail resume "command exit ${crc}, companion exit ${rc}, job ${status}: ${out:0:300}"
        fi
    else
        fail resume "agy queued something other than the requested command: $(field "${job}" queued_commands)"
    fi
else
    log "SKIP  resume: the task queued no commands"
fi

# result
start=${SECONDS}
rc=0
out="$(companion result "${task_id}")" || rc=$?
if ((rc == 0)) && [[ "${out}" == *"Files changed in the job's working copy"* && "${out}" == *"stats.py"* ]]; then
    pass result $((SECONDS - start))
else
    fail result "exit ${rc}: ${out:0:300}"
fi

# check the fix itself
start=${SECONDS}
crc=0
output="$(run_confined "${worktree}" python3 -c 'import stats; print(stats.average([1, 2, 3]))')" || crc=$?
if ((crc == 0)) && [[ "$(printf '%s' "${output}" | tail -n 1)" == "2.0" ]]; then
    pass "check fixed average()" $((SECONDS - start))
elif ((crc == 125)); then
    log "SKIP  check fixed average(): ${output}"
else
    fail "check fixed average()" "exit ${crc}: ${output:0:300}"
fi

# apply --check
start=${SECONDS}
rc=0
out="$(companion apply --check "${task_id}" 2>&1)" || rc=$?
if ((rc == 0)) && [[ "${out}" == *"apply cleanly"* ]] && [[ "$(checkout_state)" == "${before}" ]]; then
    pass "apply --check" $((SECONDS - start))
else
    fail "apply --check" "exit ${rc}: ${out:0:300}"
fi

# cancel
start=${SECONDS}
if ! companion plan --background --model "${model}" design a plugin system for stats.py >/dev/null; then
    fail cancel "could not start a background job"
else
    job="{}"
    for _ in $(seq 1 30); do
        job="$(newest_job plan)"
        [[ -n "$(field "${job}" agy_pid)" ]] && break
        sleep 1
    done
    agy_pid="$(field "${job}" agy_pid)"
    rc=0
    companion cancel "$(field "${job}" id)" >/dev/null || rc=$?
    sleep 2
    job="$(newest_job plan)"
    if ((rc == 0)) && [[ "$(field "${job}" status)" == "cancelled" ]] && [[ -n "${agy_pid}" ]] \
        && ! kill -0 "${agy_pid}" 2>/dev/null; then
        pass cancel $((SECONDS - start))
    else
        fail cancel "exit ${rc}, status $(field "${job}" status), agy pid ${agy_pid:-none}"
    fi
fi

# Claude Code, optional
if ${with_claude}; then
    claude_bin="${CLAUDE_BIN:-$(command -v claude || true)}"
    if [[ -z "${claude_bin}" ]]; then
        fail claude "claude not found; set CLAUDE_BIN"
    else
        for command in "/agy:setup" "/agy:review --wait --model ${model}"; do
            start=${SECONDS}
            rc=0
            out="$(cd "${repo}" && "${claude_bin}" -p "${command}" --plugin-dir "${plugin}" </dev/null 2>&1)" || rc=$?
            if ((rc == 0)) && [[ -n "${out}" ]]; then
                pass "claude ${command}" $((SECONDS - start))
            else
                fail "claude ${command}" "exit ${rc}: ${out:0:300}"
            fi
        done
    fi
fi

log ""
log "Approval rounds for the task job:"
if table="$(companion status "${task_id}")"; then
    printf '%s\n' "${table}" | sed -n '/^| Round/,/^$/p' | tee -a "${report}"
else
    log "(status unavailable)"
fi
log "Report: ${report}"
if ((failures > 0)); then
    log "${failures} step(s) failed."
    exit 1
fi
log "All steps passed."
