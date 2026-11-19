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

- pending

## Finishing

- final: Main was merged into the feature branch before the final review; test_config.py conflict resolved by dropping main's two PrivacyFence audience-table size tests (their limits were removed in p2) and keeping its tech-repo dirs tests.
