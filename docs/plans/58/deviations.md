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

## p8-google-provider

- None.

## p9-keycloak-provider

- pending

## p10-directory-store

- conflict: CHANGELOG.md conflict with p6 resolved by keeping both bullets (mechanical).

## p11-directory-sync

- None.

## p12-oauth-store

- None.

## p13-oauth-provider

- retry: Sent back once: worker had reformatted about 50 files outside its touches; asked to restore them and keep only its own files.

## p14-oauth-flow

- None.

## p15-oauth-routes

- pending

## p16-caller-wiring

- pending

## p17-forgejo-membership

- pending

## p18-flow-tests

- pending

## p19-docs-design-config

- pending

## p20-docs-retire

- pending

## Final review

- pending

## Finishing

- pending
