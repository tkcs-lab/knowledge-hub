# Plan 77: amd64 release image

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** Nothing changes in how you release: run `/tld:release` as before. The next release publishes `ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z` as one image index for both `linux/arm64` and `linux/amd64`, so `docker pull` on an x86-64 host gets the amd64 image under the same tag. The release dry run builds and smoke-tests both images, and the real run fails before the publish approval if the pushed index is missing one.

**Scope:** One protected change to `.github/workflows/release.yml` (QEMU on the existing arm64 runner, a second build and smoke test for amd64, a two-platform push, a platform check after the push), delivered as its own protected-change pull request. The feature pull request changes only comments and docs (`Dockerfile` line 1, `deploy/compose.example.yaml`, `docs/deployment.md`, `docs/runbook.md`) and the changelog. The wheel is already architecture-neutral and does not change. `ci.yml`, the release check gate, the publish job and `tests/unit/test_release_files.py` do not change.

**Security:** The release workflow gains one third-party action, `docker/setup-qemu-action@v3`, referenced by major tag like the other Docker actions in the file. It runs in the `build` job, which holds `packages: write`, and by default pulls the unpinned image `tonistiigi/binfmt` and runs it privileged to register the emulator. Pinning that image is left to the owner (as for the actions themselves, which use major tags).

**Review focus:** building amd64 under QEMU on the arm64 runner instead of on a native amd64 runner, and why the test file stays as it is (3.1); the platform check that ignores the `unknown/unknown` attestation entries (3.2); the order of the two pull requests (3.4).

## 1. Goal

Today every release image is `linux/arm64` only. Anyone running the hub on an x86-64 host cannot use the published image. After this change the same tag serves both architectures. Tracking issue: [tkcs-lab/knowledge-hub#77](https://github.com/tkcs-lab/knowledge-hub/issues/77).

## 2. Current state

- `.github/workflows/release.yml` job `build` runs on `ubuntu-24.04-arm`. It builds the image with `docker/build-push-action@v6` for `platforms: linux/arm64` with `load: true` and tag `knowledge-hub:release-test` (lines 83-90). It smoke-tests the image with `docker run --rm knowledge-hub:release-test --help` (lines 92-95). When `dry_run` is false it pushes by digest, again for `linux/arm64` only (lines 122-129). Job `publish` tags that digest with `docker buildx imagetools create -t ...:$VERSION ...@$DIGEST`, which copies a single manifest or an index unchanged.
- The wheel is `py3-none-any`, so it already runs on any architecture.
- `tests/unit/test_release_files.py` (unit, run by `ci` on every pull request) pins the shape of `release.yml`: the only input is `dry_run`, top-level `permissions` is `contents: read`, jobs are exactly `build` and `publish`, `build` runs on `ubuntu-24.04-arm`, `publish` uses environment `release`, there is no `:latest` and no `skip_checks`, every `run:` starts with `set -euo pipefail`, and `TLD_RELEASE_TOKEN` appears only in `publish`.
- The `ci` workflow's `image` job builds on `ubuntu-24.04-arm` only. Its check name `image` is required by name in `release.yml` ("Require the ci checks on this commit") and in the `main` ruleset.
- Text that says arm64 only: `Dockerfile:1`, `deploy/compose.example.yaml:6`, `docs/deployment.md:35`, `docs/runbook.md:249` (Releasing step 2) and step 3.
- `.github/workflows/**` is a protected path: no worker edits it. Precedent for delivering a workflow change: [docs/plans/18/plan.md](../18/plan.md) section 3.6.

## 3. Design

### 3.1 How amd64 is built, and what was rejected

**Chosen: QEMU on the existing arm64 runner.** The `build` job adds `docker/setup-qemu-action@v3` with `platforms: amd64` before buildx. It builds each platform on its own with `load: true` (the docker image store cannot load a multi-platform image), smoke-tests both, and pushes one index for `linux/arm64,linux/amd64` by digest. `steps.push.outputs.digest` is then the index digest, and `publish` tags it unchanged. The job names, the runner, the permissions and the token placement stay as they are, so `tests/unit/test_release_files.py` passes unchanged. Only the image build runs emulated, and it compiles nothing: every native dependency (for example `psycopg[binary]`, `cryptography`, `pydantic-core`, `pyyaml`, `ruamel.yaml.clib`) ships amd64 wheels.

Rejected:

- **A matrix of native runners (`ubuntu-24.04` and `ubuntu-24.04-arm`), with `publish` merging the two digests.** It is faster and gives a native amd64 smoke test. But it changes the job shape and `runs-on`, which `test_release_files.py` asserts. The test lives on the feature branch and the workflow arrives through a separate protected pull request, so each pull request would be red until the other merged. It also needs digest artifacts passed between jobs. This is the fallback if emulated builds turn out too slow (Risks), done later as its own change with the test in the same pull request as the workflow.
- **Per-architecture tags (`X.Y.Z-amd64`).** The operator would have to choose a tag by hand, and "every kh container runs the same tag" (`deploy/compose.example.yaml:2`) would depend on the host. Rejected.
- **An amd64 job in `ci.yml`.** That would be a second protected pull request. The release dry run already builds and smoke-tests amd64 before anything is pushed, so the job only moves the warning earlier. Turning `image` into a matrix would rename the check to `image (...)` and break the release gate and the ruleset. Left to the follow-up issue (3.3 item 6).
- **Pushing the exact tested images instead of rebuilding.** The push rebuilds today, too. Both builds and the push run in the same job and buildx builder, so the push reuses the layer cache. The platform check (3.2) catches a missing platform. Not changed.

### 3.2 The protected change to `.github/workflows/release.yml`

The worker reproduces this diff exactly (paths relative to the repository root, no extended headers). It applies to `release.yml` at `main` `b1ce319`, and `tests/unit/test_release_files.py` passes against the patched file. Every new `run:` starts with `set -euo pipefail`. The "Verify image platforms" step reads the raw index and ignores entries without a `platform` field. It also ignores the extra `unknown/unknown` entries that buildx adds for provenance attestations. It fails only when `linux/arm64` or `linux/amd64` is missing. `{{.Architecture}}` is a Docker template, not an Actions expression (no `$`).

`````diff
--- a/.github/workflows/release.yml
+++ b/.github/workflows/release.yml
@@ -78,21 +78,37 @@ jobs:
             exit 1
           fi
 
+      - uses: docker/setup-qemu-action@v3
+        with:
+          platforms: amd64
+
       - uses: docker/setup-buildx-action@v3
 
-      - name: Build image
+      - name: Build image (arm64)
         uses: docker/build-push-action@v6
         with:
           context: .
           platforms: linux/arm64
           load: true
           push: false
-          tags: knowledge-hub:release-test
+          tags: knowledge-hub:release-test-arm64
+
+      - name: Build image (amd64)
+        uses: docker/build-push-action@v6
+        with:
+          context: .
+          platforms: linux/amd64
+          load: true
+          push: false
+          tags: knowledge-hub:release-test-amd64
 
       - name: Smoke test
         run: |
           set -euo pipefail
-          docker run --rm knowledge-hub:release-test --help
+          for arch in arm64 amd64; do
+            docker run --rm --platform "linux/$arch" "knowledge-hub:release-test-$arch" --help
+            test "$(docker image inspect --format '{{.Architecture}}' "knowledge-hub:release-test-$arch")" = "$arch"
+          done
 
       - name: Build and smoke-test the wheel
         run: |
@@ -125,9 +141,31 @@ jobs:
         uses: docker/build-push-action@v6
         with:
           context: .
-          platforms: linux/arm64
+          platforms: linux/arm64,linux/amd64
           outputs: type=image,name=ghcr.io/${{ github.repository }},push-by-digest=true,name-canonical=true,push=true
 
+      - name: Verify image platforms
+        if: inputs.dry_run == false
+        env:
+          IMAGE: ghcr.io/${{ github.repository }}
+          DIGEST: ${{ steps.push.outputs.digest }}
+        run: |
+          set -euo pipefail
+          docker buildx imagetools inspect --raw "$IMAGE@$DIGEST" > "$RUNNER_TEMP/index.json"
+          python3 - "$RUNNER_TEMP/index.json" <<'PY'
+          import json, sys
+          index = json.load(open(sys.argv[1]))
+          found = {
+              m["platform"]["os"] + "/" + m["platform"]["architecture"]
+              for m in index.get("manifests", [])
+              if "platform" in m
+          }
+          missing = {"linux/arm64", "linux/amd64"} - found
+          print("platforms:", " ".join(sorted(found)))
+          if missing:
+              sys.exit("image index lacks " + " ".join(sorted(missing)))
+          PY
+
   publish:
     needs: build
     if: inputs.dry_run == false
`````

Report line: `protected-change: .github/workflows/release.yml - build and push linux/amd64 next to linux/arm64 (plan 77, 3.2)`.

### 3.3 The feature pull request: comments, docs, changelog, follow-up

Literal edits. Each old text occurs exactly once today.

1. `Dockerfile` line 1: replace `# Multi-stage, architecture-neutral image (built natively on arm64 in the release workflow).` with `# Multi-stage, architecture-neutral image (the release workflow builds linux/arm64 natively and linux/amd64 under QEMU).`
2. `deploy/compose.example.yaml` line 6: replace `    # The release image (linux/arm64). Pin the digest in the stack:` with `    # The release image (one tag for linux/arm64 and linux/amd64). Pin the digest in the stack:`
3. `docs/deployment.md` (Dependencies table, Python row): replace ``the release image is `linux/arm64` only`` with ``the release image is `linux/arm64` and `linux/amd64` under one tag``.
4. `docs/runbook.md`, section Releasing:
   - step 2: replace `builds the arm64 image and the wheel and runs a smoke test.` with `builds the arm64 image, the amd64 image (under QEMU) and the wheel and smoke-tests each.`
   - step 3: replace ``The `build` job pushes the image to GHCR by digest only.`` with ``The `build` job pushes one image index for linux/arm64 and linux/amd64 to GHCR by digest only, and fails unless the index holds both platforms.``
   - step 5: at the end of the line that contains ``Check the digest with `docker buildx imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`.``, append on the same line ` The tag is one index for both platforms; Docker pulls the one that matches the host.` (with the leading space).
   - the last paragraph of Releasing: replace ``The change to `.github/workflows/release.yml` that builds and attaches the wheel is a protected path: it reaches `main` as a protected-change pull request, reviewed and merged by a person.`` with ``Changes to `.github/workflows/release.yml` (the wheel, the amd64 image) are a protected path: each reaches `main` as a protected-change pull request, reviewed and merged by a person.``
5. `CHANGELOG.md`: directly under `## [Unreleased]` add a blank line, `### Added`, a blank line and this bullet: ``- Releases: the GHCR image is also built for linux/amd64 (under QEMU on the arm64 runner), and one tag `X.Y.Z` serves both platforms. The release dry run smoke-tests both images, and the real run checks that the pushed index holds both. The `release.yml` change reaches `main` as a protected-change pull request.`` Open no version heading.
6. One follow-up issue in tkcs-lab/knowledge-hub, title `Pin the release image platforms in a unit test; optional amd64 image job in ci`, body: `After the protected-change pull request of #77 is merged: (1) in tests/unit/test_release_files.py assert that the build job's "Push image by digest" step has platforms linux/arm64,linux/amd64 and that a "Verify image platforms" step follows it; (2) decide whether ci.yml gets a separate image-amd64 job on ubuntu-24.04 (keep the image check name unchanged: the release gate and the main ruleset require it by name).`

### 3.4 Delivery and order

1. Phase p1 makes the edits of 3.3 on the feature branch and reports the protected change of 3.2 with its diff.
2. The implement orchestrator puts the diff to the owner (`apply`, `skip`, `stop`). `apply` is not marked recommended, because the path is a workflow. After `apply` it opens the draft pull request `Protected change: .github/workflows/release.yml` to `main`. That pull request's `ci` run includes `tests/unit/test_release_files.py` against the new file.
3. **Order:** merge the protected pull request first, then the feature pull request. In the other order the docs would describe an amd64 image that the workflow does not build yet. Nothing breaks, but the docs would be wrong until the protected pull request merges.
4. Then ma1.

## 4. Manual steps

### Before implementation

None.

### After implementation

**ma1. A release dry run builds and smoke-tests both images** (about 15 minutes, most of it waiting). Do it after the protected pull request is merged to `main`.

1. Wait until the `ci` run on `main`'s newest commit is green: [Actions → ci](https://github.com/tkcs-lab/knowledge-hub/actions/workflows/ci.yml).
2. Open [Actions → release](https://github.com/tkcs-lab/knowledge-hub/actions/workflows/release.yml), click **Run workflow**, keep branch `main` with `dry_run` ticked, and click **Run workflow**.
3. Open the run, then the `build` job. The steps **Build image (arm64)**, **Build image (amd64)** and **Smoke test** pass, and the **Smoke test** log shows the `kh --help` output twice. Note the duration of **Build image (amd64)**.
4. Report in the feature pull request: `ma1: pass (dry run green, amd64 build <minutes> min)` or `ma1: fail (<step and message>)`.

The real run's **Verify image platforms** step runs at the next release. Its log line `platforms: linux/amd64 linux/arm64 ...` is the proof that the pushed index holds both platforms.

## 5. Risks and open questions

- **QEMU on an arm64 host.** `docker/setup-qemu-action` registers x86-64 emulation through the multi-arch `tonistiigi/binfmt` image. This was not verified from the planning environment. If **Build image (amd64)** fails with `exec format error`, or takes more than about 10 minutes in ma1, the fix is the native-runner matrix of 3.1, done as a new plan. Do not weaken the step.
- **Index format.** The check assumes that `imagetools inspect --raw` of a multi-platform push prints an OCI index with a `manifests` list whose entries carry `platform.os` and `platform.architecture`. It was checked only against a hand-written sample with an `unknown/unknown` attestation entry. If the first real release fails at **Verify image platforms** while the log shows both platforms, the check is wrong. The fix is a new protected-change diff, and the image is not published, because `publish` needs `build`. The dry run never reaches the two-platform push or this check, so they run for the first time in a real release; a failed check leaves an untagged digest in GHCR, which is harmless and can be deleted in the package settings.
- **Order of the pull requests** (3.4). If the owner skips the protected change, the orchestrator records `protected-change skipped: .github/workflows/release.yml`, and the docs of 3.3 would then be wrong. The feature pull request must not merge until the protected one is on `main`, or until its docs are reverted.
- **A brief that is wrong.** If the diff of 3.2 does not apply with `patch --dry-run` to `release.yml` at `origin/main`, or the patched copy fails the copied release test, `release.yml` changed since this plan. Likewise if an old text of 3.3 is not found exactly once. In each case stop with `status=blocked`.
- Architect findings, all non-blocking, are taken into 3.1 to 3.3: update the arm64-only texts; the push rebuilds rather than pushing the tested images; `ci` covers arm64 only. It also asked whether pip runs reliably under QEMU on an arm64 host. That is the first risk above, and ma1 settles it, so there is no research request.

## Implementation manifest

```yaml
plan_slug: amd64-release
feature_branch: feat/amd64-release
tracking_issue: 77
max_parallel: 1
manual_before: []
manual_after:
  - id: ma1-release-dry-run
    title: Run a release dry run on main and see the arm64 and amd64 builds and smoke tests pass
    why: only GitHub's arm64 runner with QEMU can prove the emulated amd64 build works; the profile has no ci.dispatchable, so implement cannot dispatch release.yml itself
verify_after_merge:
  - .venv/bin/python -m pytest -q -m unit
final_checks:
  - "`tld plan deviations check --file docs/plans/77/deviations.md --plan docs/plans/77/plan.md --all` exits 0"
  - the changelog has entries under "## [Unreleased]" and no version heading
  - .github/workflows/release.yml has a protected-change pull request or a `protected-change skipped` deviation
phases:
  - id: p1-amd64-release
    title: Report the release.yml amd64 change as a protected change; Dockerfile comment, compose comment, docs, changelog
    depends_on: []
    complexity: S
    touches:
      - Dockerfile
      - deploy/compose.example.yaml
      - docs/deployment.md
      - docs/runbook.md
      - CHANGELOG.md
    brief: |
      Read first: docs/plans/77/plan.md sections 3.2, 3.3, 3.4; README.md; .github/workflows/release.yml; tests/unit/test_release_files.py.
      Do not edit anything under .github/ (protected). Build the diff in a scratch directory outside the repository.
      1. Copy the diff of plan 3.2 (the content of the fenced diff block, without its two fence lines) byte for byte to <scratch>/release.diff. Its first two lines are exactly `--- a/.github/workflows/release.yml` and `+++ b/.github/workflows/release.yml`.
      2. Run `git show origin/main:.github/workflows/release.yml > <scratch>/release.yml`, then `patch --dry-run <scratch>/release.yml < <scratch>/release.diff` and `patch <scratch>/release.yml < <scratch>/release.diff`. If either fails, stop with status=blocked (release.yml changed since the plan).
      3. Check the patched copy against the release test: `sed "s#^WORKFLOW = .*#WORKFLOW = Path('<scratch>/release.yml')#" tests/unit/test_release_files.py > <scratch>/test_release_copy.py`, then `.venv/bin/python -m pytest -q -p no:cacheprovider <scratch>/test_release_copy.py` must report 6 passed. Then check the step order: `.venv/bin/python -c 'import sys, yaml; s = [x.get("name") or x.get("uses") for x in yaml.safe_load(open(sys.argv[1]))["jobs"]["build"]["steps"]]; i = s.index("docker/setup-qemu-action@v3"); assert s[i:i + 4] == ["docker/setup-qemu-action@v3", "docker/setup-buildx-action@v3", "Build image (arm64)", "Build image (amd64)"], s; assert s[-1] == "Verify image platforms", s' <scratch>/release.yml`. If a check fails, stop with status=blocked.
      4. Check the platform check offline. Extract the step's script without its docker line: `.venv/bin/python -c 'import sys, yaml; r = [x for x in yaml.safe_load(open(sys.argv[1]))["jobs"]["build"]["steps"] if x.get("name") == "Verify image platforms"][0]["run"]; open(sys.argv[2], "w").write("\n".join(":" if l.startswith("docker buildx imagetools inspect") else l for l in r.splitlines()) + "\n")' <scratch>/release.yml <scratch>/verify.sh`. Write <scratch>/index.json as `{"manifests": [{"platform": {"os": "linux", "architecture": "arm64"}}, {"platform": {"os": "linux", "architecture": "amd64"}}, {"platform": {"os": "unknown", "architecture": "unknown"}}]}` and run `RUNNER_TEMP=<scratch> bash <scratch>/verify.sh`: it exits 0 and prints `platforms: linux/amd64 linux/arm64 unknown/unknown`. Remove the amd64 entry from index.json and run it again: it exits 1 and prints `image index lacks linux/amd64`. Otherwise stop with status=blocked.
      5. In your report put the line `protected-change: .github/workflows/release.yml - build and push linux/amd64 next to linux/arm64 (plan 77, 3.2)` followed by <scratch>/release.diff in a fenced block of five backticks.
      6. Make the literal edits of plan 3.3 items 1 to 5 in Dockerfile, deploy/compose.example.yaml, docs/deployment.md, docs/runbook.md and CHANGELOG.md. If an old text is not found exactly once, stop with status=blocked. Open no version heading in CHANGELOG.md.
      7. Open the follow-up issue of plan 3.3 item 6 in tkcs-lab/knowledge-hub with the GitHub issue tool, title and body verbatim. Put its URL in your report as `follow-up: <url>`. If issue creation is refused, put `follow-up: not created` and go on.
      8. `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest -q -m unit` pass (tests/unit/test_deploy_files.py checks the Dockerfile and the compose file; tests/unit/test_docs_links.py checks doc links and length). Do not edit any test file.
    acceptance:
      - grep -c "linux/amd64 under QEMU" Dockerfile prints 1
      - grep -c "one tag for linux/arm64 and linux/amd64" deploy/compose.example.yaml prints 1
      - grep -c "under one tag" docs/deployment.md prints 1
      - grep -c "the amd64 image (under QEMU)" docs/runbook.md prints 1
      - grep -c "fails unless the index holds both platforms" docs/runbook.md prints 1
      - grep -c "Docker pulls the one that matches the host" docs/runbook.md prints 1
      - grep -c 'imagetools inspect ghcr.io/tkcs-lab/knowledge-hub:X.Y.Z`. The tag is one index' docs/runbook.md prints 1
      - grep -c '`linux/arm64` only' docs/deployment.md prints 0
      - grep -c 'built natively on arm64 in the release workflow' Dockerfile prints 0
      - grep -c '(linux/arm64). Pin the digest' deploy/compose.example.yaml prints 0
      - grep -c 'builds the arm64 image and the wheel' docs/runbook.md prints 0
      - grep -c 'pushes the image to GHCR by digest only' docs/runbook.md prints 0
      - grep -c 'that builds and attaches the wheel is a protected path' docs/runbook.md prints 0
      - awk '/^## \[Unreleased\]/{f=1;next} /^## \[0\./{f=0} f' CHANGELOG.md | grep -c "the GHCR image is also built for linux/amd64" prints 1
      - git diff --name-only origin/main...HEAD lists only Dockerfile, deploy/compose.example.yaml, docs/deployment.md, docs/runbook.md, CHANGELOG.md and files under docs/plans/77/
      - the report has the protected-change line of step 5, followed by a diff that applies cleanly with `patch --dry-run <copy of origin/main's release.yml> < <the diff>`
      - .venv/bin/python -m ruff check . and .venv/bin/python -m pytest -q -m unit pass
      - the report has a line that starts with the word follow-up and a colon
```
