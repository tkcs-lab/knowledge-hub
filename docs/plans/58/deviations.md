# Deviations: oidc-oauth-directory

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-caller-middleware

- worker: Worker deleted two PrivacyFence-claim tests (claim audiences in test_http_e2e, jwks-url settings error in test_wiring) besides the files the brief names; integration tests were converted but not run (no pgvector here).
- retry: Sent back once: tip commit lacked the Plan-Phase trailer; owner chose unit-only verification (integration tests left to CI).
- scope: src/knowledge_hub/mcp/errors.py: unused AUDIENCES_SERVICE_ONLY constant removed outside touches; collides with p2 and p6.

## p2-remove-privacyfence

- None.

## p3-config-settings

- None.

## p4-migration-identity

- None.
- conflict: CHANGELOG.md merge conflict with p3 resolved by keeping both entries (mechanical).

## p5-directory-core

- None.

## p6-hub-keys

- None.

## p7-oidc-relying-party

- None.
- final: JWKS is refreshed by a 5-minute TTL, not only on an unknown kid.

## p8-google-provider

- None.
- final: The 60-second retry deadline applies to each request, not to the whole fetch (same in the Keycloak provider).
- final: Final review blocking: users in domains other than the subject's were enabled without a check; fix-3.

## p9-keycloak-provider

- None.
- final: The Keycloak URL rule is checked in directory/keycloak.py when the provider is built, not in Settings.

## p10-directory-store

- conflict: CHANGELOG.md conflict with p6 resolved by keeping both bullets (mechanical).

## p11-directory-sync

- None.
- final: kh directory sync --every requires a number and never falls back to refresh_seconds; kh directory show prints the effective membership counts, not the snapshot counts.

## p12-oauth-store

- None.

## p13-oauth-provider

- retry: Sent back once: worker had reformatted about 50 files outside its touches; asked to restore them and keep only its own files.
- None.

## p14-oauth-flow

- None.
- final: When the stored redirect URI has left the allowlist, the callback and consent POST answer 400 (FlowError) instead of redirecting with access_denied; kept as the safer choice, so no redirect goes to an untrusted URI.
- final: issue_code takes a third argument client_id instead of (request, email).

## p15-oauth-routes

- None.

## p16-caller-wiring

- None.
- final: Final review blocking: host guard compared the raw Host header; fix-1 compares hostnames only.
- final: fix-1: a request with no or empty Host header is treated as the public name for the three non-public paths (fail closed); on a public_url deployment an internal probe without a Host header gets 404 for /healthz.

## p17-forgejo-membership

- None.
- conflict: CHANGELOG.md conflict resolved by keeping both bullets (mechanical).
- final: Final review blocking: members with a mismatching Forgejo email were not removed from teams; fix-2.

## p18-flow-tests

- None.

## p19-docs-design-config

- None.

## p20-docs-retire

- None.

## Final review

- final: Review 1, blocking: public-host guard compared the raw Host header (ports, trailing dot, missing Host); fixed by fix-1.
- final: Review 1, blocking: Forgejo team members with a mismatching directory email were not removed; fixed by fix-2.
- final: Review 1, blocking: Google users outside the subject's domain were enabled unchecked; fixed by fix-3.
- final: Review 1, blocking: test_wiring asserted deps.bearer is None after p16 always builds a resolver; fixed by fix-1.
- final: Review 1, blocking: deviations of p3, p7, p8, p9, p11 and p14 were missing from this file; recorded by the orchestrator. Review 2 approved.

## Finishing

- final: Main was merged into the feature branch before the final review; test_config.py conflict resolved by dropping main's two PrivacyFence audience-table size tests (their limits were removed in p2) and keeping its tech-repo dirs tests.
- final: Scope disposition: the one recorded out-of-scope path (src/knowledge_hub/mcp/errors.py, p1) is accepted; no other phase drifted. The three fix branches were not scope-checked (each touched only the files its brief named).
- final: Verification gap: no integration test or SQL ran in any session (no PostgreSQL with pgvector; owner decision to rely on CI). Only ruff and the unit tests ran: 1009 passed.
- final: fix-4 (after the first CI run of the integration tests on the pull request): local Deps fixtures lacked the bearer resolver, the OAuth test app wrapped by the host guard never started its lifespan, one test config had a non-member approver, and test_migration_0006 leaked the cluster-wide role kh_nobody. Test code only.
