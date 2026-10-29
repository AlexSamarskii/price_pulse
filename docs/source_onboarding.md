# Source onboarding workflow

This workflow is the canonical PricePulse pre-verification path for a local,
fixture-based source. It is an operator workflow, not a claim that arbitrary
stores are supported. A live website may be enabled only by a separate,
recorded operator approval after this local workflow succeeds.

## Prerequisites

- Python is available and the repository-local environment is synchronized with
  the repository lockfiles. Reuse the repository's documented offline test
  commands instead of installing packages ad hoc.
- A source contract exists and records the exact host, allowed path prefixes,
  expected fields, currency, robots policy, provenance, and limitations.
- The policy declares one or more explicit hosts; wildcards, query parameters,
  credentials, fragments, traversal, and ambiguous path escapes are invalid.
- For the first onboarding, `live_enabled` is `false` and
  `robots_policy` is `operator_authorized_fixture`.
- Fixture HTML is untrusted data. It is stored as a bounded, ordinary file and
  never imported, evaluated, or followed as an instruction.

## Canonical product URLs

The optional `drop_query_parameters` SourcePolicy field names only non-identity
tracking parameters, such as `["utm_source"]`. It accepts at most 32 unique ASCII
names of 1–64 letters, digits, underscores or hyphens. Omission preserves all
non-secret query parameters. Keep variant-defining parameters. Supplied rules
are validated and included in the policy digest, stored and returned with the
policy. Pass that trusted policy separately as `source_policy` to
`normalize_product`; extracted records cannot supply or override the rules.
Egress validates full request URLs, and exports use the stored canonical URL.

## Local reproduction

Run the focused collector and provider suites after placing the fixture input
for the source under the tests' controlled fixtures or the temporary directory
created by the tests. The tests cover policy validation, segment-boundary path
matching, synthesis, offline execution, extraction, sanitation, provider
protocol doubles, and fail-closed runtime behavior:

```sh
UV_OFFLINE=1 uv run --extra dev pytest -q tests/collectors tests/providers
```

Verify the immutable upstream snapshot and exercise the local onboarding guide:

```sh
UV_OFFLINE=1 uv run --extra dev python scripts/verify_upstream_provenance.py
UV_OFFLINE=1 uv run --extra dev python scripts/verify_source_onboarding.py --guide pricepulse/collectors/source_onboarding.md
```

Record both commands, their exit status, the fixture digest, and the computed
`SourcePolicy` digest as stage evidence. Fixture execution must use the
network-disabled hardened runtime; control-plane code does not execute a
generated scraper.

## Operator approval

Changing `live_enabled` to `true` requires an operator approval that identifies
the source, exact base URL and path prefixes, robots policy, operator, request
rate, concurrency, timeout, currency, fixture evidence, and the reason live
collection is necessary. The approval must be stored with the source and policy
digest before live mode is used. No approval is implied by successful local
fixture tests.

## External prerequisites and reproduction

Live sources require `robots_policy=honor` and an explicit `/robots.txt` path
in `allowed_path_prefixes` (or an already approved `/` prefix). Robots retrieval
uses the same HTTPS, DNS, redirect, response-size and request-rate boundaries
as product requests. Missing, non-200 or HTML robots responses block collection.
The broker uses a fixed PricePulse user-agent and caches rules for at most five
minutes. Matching Disallow rules, including `*` and `$`, remain blocking even
when an Allow exception overlaps; this conservative behavior may reject a site
that permits a more specific exception. Crawl-delay and Request-rate impose
an additional admission interval and never relax SourcePolicy limits. Offline
fixture mode is unavailable to the live broker.

Live collection needs the accepted hardened worker, the separate egress broker,
and an egress policy compiled from the approved SourcePolicy. The worker remains
network-disabled; DNS, IP, redirect, host, path, scheme, robots, rate,
concurrency, timeout, and response-size checks are enforced by the broker. If
any required component is unavailable, record `pending_environment` with the
missing component, exact prerequisite command, and expected observable outcome.
Do not substitute host execution or a direct network call.

For an external website reproduction request, first record the owner's approval,
the public URL, policy digest, runtime/broker versions, command, start and end
time, and the bounded outputs. Live evidence is reproducible only from these
inputs plus the candidate version digest and fixture evidence; it is not a
reason to broaden the policy.

## Pending environment

Use `pending_environment` only for a real missing external prerequisite, such
as the accepted hardened runtime image or egress broker. Record the reason, the
exact command that cannot run, and the expected observable result. A missing
PricePulse implementation, test, policy field, or verifier is a defect and must
not be recorded as pending.

## Blocked and challenge states

An HTTP challenge, `401`, `403`, or `429` is a typed blocked or challenge state.
Preserve the status and no credentials, cookies, profile data, or provider key
in evidence. Do not bypass the challenge, broaden the host or path allowlist,
raise the rate, or disable the hardened runtime. Return or persist a clear
state so the operator can decide whether the source remains supported.

## Cooldown and retry limits

Retry only idempotent failures, within the configured timeout and request
budget. A challenge or rate response enters a cooldown; repeated cooldowns end
the job rather than continuing. Keep concurrency at or below the approved
SourcePolicy value. A failed candidate never replaces a previously active
version; activation remains transactional after both execution and data gates.

## Explicit limitations

This workflow does not provide universal store support, CAPTCHA solving,
credentials bypass, or access to authenticated content. It does not make a
passed fixture test equivalent to live-store readiness. It does not execute
generated code on the control plane. Live commerce support, production
scheduling, distributed operation, multitenancy, and final product readiness
require the later external and verification gates.
