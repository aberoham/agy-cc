# Using agy with a Gemini Enterprise licence

This plugin drives agy headlessly. That works only after agy is signed in
with a licence that includes it. This page collects what that takes for an
organisation's **Gemini Enterprise** licence (the product formerly called
Google Agentspace), and the quirks we hit. It was written in October 2026
against agy 1.4.7; check the linked pages for changes.

Each point says where it comes from: **observed** (seen on our machine),
**Google docs** (Google's documentation), or **reported** (an issue in
[google-antigravity/antigravity-cli](https://github.com/google-antigravity/antigravity-cli)).
Placeholders are in capitals.

## Names you will meet

- Google Agentspace is now part of **Gemini Enterprise**
  ([Google blog](https://blog.google/feed/google-agentspace/)).
- Vertex AI is now **Gemini Enterprise Agent Platform**, or Agent Platform
  ([Google docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/vertex-ai-name-changes)).
  Agent Platform pay-as-you-go billing is separate from a Gemini Enterprise seat.
- Google calls the entitlement that covers Antigravity and agy **AI developer
  tools**
  ([Google docs](https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools-overview)).

## What the organisation needs

- **An edition that includes AI developer tools.** Google docs list
  Standard, Plus, Standard Emerging Market (for eligible customers) and
  Pay-as-you-go, and say that Business and Frontline do not include it. The
  project needs an invoiced Cloud Billing account
  ([overview](https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools-overview),
  [editions](https://docs.cloud.google.com/gemini/enterprise/docs/editions)).
- **AI developer tools turned on, per location.** Gemini Enterprise →
  Settings → AI developer tools. Google docs say it is on by default for new
  subscriptions and must be turned on for existing ones
  ([settings](https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools-settings)).
- **APIs enabled on the project:** `discoveryengine.googleapis.com`,
  `businessaicode.googleapis.com` and `aiplatform.googleapis.com`. Google
  docs say to enable `aiplatform` before buying licences
  ([Antigravity enterprise docs](https://antigravity.google/docs/enterprise/)).
  A missing `businessaicode` API returns `403 SERVICE_DISABLED` (reported,
  [issue 1110](https://github.com/google-antigravity/antigravity-cli/issues/1110)).
- **A role for every user:** `roles/discoveryengine.agentspaceUser` (Gemini
  Enterprise User), administrators included. The admin role
  (`roles/discoveryengine.agentspaceAdmin`) configures but does not grant use
  (Google docs, [enterprise](https://antigravity.google/docs/enterprise/)).
- **A seat for the user, in the right project and location.** Seats are
  scoped to a project and a location (`global`, `us` or `eu`). An
  administrator assigns them, or turns on automatic assignment at first
  sign-in ([licenses](https://docs.cloud.google.com/gemini/enterprise/docs/licenses)).
- **VPC Service Controls**, if used: add `businessaicode` and `aiplatform`
  to the perimeter (Google docs, [enterprise](https://antigravity.google/docs/enterprise/)).

## Signing in

- **Sign in once, interactively, before using this plugin.** Print mode uses
  the saved sign-in; without one it exits with `authentication required`
  (observed; [headless docs](https://antigravity.google/docs/cli/headless/)).
  The plugin cannot sign in for you: run `agy` in a terminal window.
- **The flow:** Business account → Continue with Google Cloud. With workforce
  identity federation (sign-in through your own identity provider), choose
  Advanced WIF Configuration and enter
  `locations/global/workforcePools/POOL_ID/providers/PROVIDER_ID` (Google
  docs, [enterprise](https://antigravity.google/docs/enterprise/)). The labels
  have changed between releases: older versions said "Use advanced SSO
  config" or "Use a Google Cloud project"
  ([1.1.10 release](https://github.com/google-antigravity/antigravity-cli/releases/tag/1.1.10),
  [issue 1037](https://github.com/google-antigravity/antigravity-cli/issues/1037)).
  An organisation can make this easier by putting the provider string on the
  user's clipboard before the first sign-in (observed: our managed install
  does this).
- **Project and location** end up in `~/.gemini/antigravity-cli/settings.json`
  as `{"gcp": {"project": "PROJECT_ID", "location": "REGION"}}`. That project
  receives agy's telemetry and model logs. Switching project means signing out
  and in again (Google docs, [install](https://antigravity.google/docs/install/),
  [enterprise](https://antigravity.google/docs/enterprise/)). `/agy:setup`
  shows the project agy will use.
- **Regional endpoints.** With location `eu` or `us`, model requests go to
  `businessaicode.REGION.rep.googleapis.com` (observed for `eu`). Sign-in
  still uses the global `businessaicode.googleapis.com` and
  `cloudcode-pa.googleapis.com`. Private Google Access addresses present a
  certificate for `*.googleapis.com`, which does not cover the `.rep`
  hostnames; Google docs describe a regional Private Service Connect endpoint
  and private DNS zone as the fix
  ([enterprise](https://antigravity.google/docs/enterprise/)).

## Quirks

- **The sign-in token can be too large for the macOS keychain.** The log
  shows `data passed to Set was too big` and agy asks for sign-in at every
  launch. Setting `WSL_INTEROP=1` makes agy store the token in
  `~/.gemini/antigravity-cli/antigravity-oauth-token` instead (reported,
  [issue 1037](https://github.com/google-antigravity/antigravity-cli/issues/1037);
  observed: our managed install wraps agy with this). The 1.4.6 changelog
  says it is fixed; we have not tested without the workaround.
  That file holds a refresh token: treat it as a credential.
- **Seat detection can fail with workforce identity federation.** agy's
  self-assignment call (`SelfAssignLicense`) returns `400 FAILED_PRECONDITION`
  even when a seat is assigned (reported,
  [issue 1110](https://github.com/google-antigravity/antigravity-cli/issues/1110)).
- **agy can fall back to pay-as-you-go.** After signing in again, some users
  were offered only "Agent Platform - Pay as you go", and one was billed that
  way while agy showed "Business" (reported,
  [issue 917](https://github.com/google-antigravity/antigravity-cli/issues/917)).
  Check what agy says it is using after every new sign-in.
- **Headless runs can lose the sign-in.** Reports include a mid-turn
  `UNAUTHENTICATED (code 401)` after a token refresh
  ([issue 1091](https://github.com/google-antigravity/antigravity-cli/issues/1091)),
  `authentication timed out` in print mode
  ([issue 944](https://github.com/google-antigravity/antigravity-cli/issues/944)),
  and a container process not loading the token file
  ([issue 479](https://github.com/google-antigravity/antigravity-cli/issues/479)).
  A failed job shows agy's last error lines; sign in again and resume.
- **Some features are not offered to business accounts.** Auto mode is
  unavailable for business and enterprise accounts (Google docs,
  [modes](https://antigravity.google/docs/cli/modes/)). The plugin does not
  use it.
- **Application Default Credentials are a different billing path.**
  `gcloud auth application-default login` plus `AGY_ADC_AUTH=true` signs agy
  in without the interactive flow (Google docs,
  [enterprise](https://antigravity.google/docs/enterprise/)). Google's docs
  and reports suggest this bills Agent Platform usage rather than a seat; we
  have not confirmed it. The plugin does not set `AGY_ADC_AUTH`.
- **Model availability depends on the licence.** Run `agy models` to see what
  your seat offers. The plugin's default is `gemini-4-argon-medium`; pass
  `--model` for another.

## Not confirmed

- Whether the `WSL_INTEROP=1` workaround is still needed on agy 1.4.6 or later.
- Exactly what makes `SelfAssignLicense` fail.
- Whether Application Default Credentials can ever use a seat.
- Whether Google Workspace accounts have a separate sign-in path.
