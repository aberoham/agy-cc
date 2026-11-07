"""Why an agy job failed, as one of a fixed set of classes with fixed exit codes.

A failed job records its class as "failure", and the companion exits with
that class's code, so Claude Code or a script can act on the kind of failure
(sign in again, start Colima, wait for quota) without parsing agy's prose.
The idea, and the codes for the classes both plugins share (quota, sign-in,
timeout, agy missing, model), come from yuting0624/antigravity-for-claude-code.

Matching reads agy's stderr, agy's own log and its error text, never the
model's reply, so a review that quotes an error message cannot change the
class.
"""

from __future__ import annotations

EXIT_CODES = {
    "agy-failed": 1,  # agy failed for a reason none of the classes below name
    # 2 is a usage error: a bad argument, or no repository
    "quota": 10,
    "signin": 11,
    "timeout": 12,
    "agy-missing": 13,
    "model": 14,
    "licence": 15,
    "stall": 16,
    "container": 17,
}

# When Google reclaims the Gemini Enterprise seat behind a saved sign-in,
# agy cannot list its models and rejects every model name as unknown. The
# reason appears only in agy's log, as this error code on a 403 response.
LICENCE_NOT_VALID = "GEMINI_ENTERPRISE_LICENSE_NOT_VALID"
# The workforce pool's session ended. agy then retries the refresh without
# end (86,296 such log lines were found across this machine's agy logs on
# 7 November 2026), so a job would otherwise wait for the stall timer.
SESSION_EXPIRED = "Refresh token has expired"

# Lower-case substrings. Each was observed in agy's log, stderr or result,
# or in docker's error output, unless its comment says otherwise.
_PATTERNS = (
    ("licence", (LICENCE_NOT_VALID.lower(),)),
    (
        "signin",
        (
            "authentication required",
            "authentication timed out",
            "not signed in",
            "please sign in",
            SESSION_EXPIRED.lower(),
        ),
    ),
    # Google's status for an exhausted quota, and its HTTP code; not yet
    # seen from agy here.
    ("quota", ("resource_exhausted", "code 429")),
    ("model", ("is not recognized as a known model",)),
    (
        "container",
        (
            "failed to connect to the docker api",
            "cannot connect to the docker daemon",
            "unable to find image",
            "no such image",
        ),
    ),
)

ADVICE = {
    "signin": "agy is not signed in, or the sign-in expired. Sign in with `agy` in a terminal, then resume this job.",
    "licence": (
        f"Google rejected agy's Gemini Enterprise licence ({LICENCE_NOT_VALID}), so agy could not list its "
        "models. Run `agy` in a terminal, type /logout, sign in again and choose a valid licence, then resume "
        "this job."
    ),
    "quota": "Google refused the request for quota. Wait, then resume this job.",
    "container": (
        "Docker could not run the job's container. Start Colima (`colima start`) or Docker Desktop, "
        "rebuild a missing image with containers/build.sh, then start the job again."
    ),
}


def classify(outcome_kind: str, *texts: str) -> str:
    """The failure class of a finished turn.

    Args:
        outcome_kind: how the turn ended: "result", "exited", "stall" or "timeout".
        *texts: agy's stderr, its log and its error text for the round.
    """
    haystack = "\n".join(texts).lower()
    for name, needles in _PATTERNS:
        if any(needle in haystack for needle in needles):
            return name
    if outcome_kind in ("stall", "timeout"):
        return outcome_kind
    return "agy-failed"


def exit_code(name: str | None) -> int:
    return EXIT_CODES.get(name or "agy-failed", 1)
