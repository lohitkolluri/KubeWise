# kwctl install — release the build when an install already exists

**Date:** 2026-07-11
**Status:** Approved (design)
**Author:** Sisyphus (orchestration), Lohit Kolluri (decisions)

## Context

`kwctl install` today is idempotent at the config layer — the Helm path runs
`helm upgrade --install kubewise` and the kustomize path runs `kubectl apply -k`,
both of which update an existing install. But they re-apply the **published**
agent image (`ghcr.io/lohitkolluri/kubewise-agent:latest`), never rebuilding from
local source. That is why a developer's local fixes (e.g. the `/api/v1/auth`
middleware fix) never reach the running agent: the deployed image is stale
relative to the code.

The user wants `kwctl install`, when an installation already exists, to **release
the current code** — commit, push, and tag `v1.0.3` — instead of doing a fresh
install. The `v1.0.3` tag triggers the existing `release.yml` CI workflow, which
builds and pushes `ghcr.io/lohitkolluri/kubewise-agent:1.0.3` (and `:latest`) plus
the forecaster image and repackages the Helm chart. `kwctl install` then redeploys
the existing release to the new image tag, so the running agent reflects the code.

## Goal

When `kwctl install` is run and a KubeWise installation is already present, it
performs a git release (commit + push + tag) of the current tree and redeploys the
existing installation to the newly published image — rather than re-applying the
published `:latest` image.

## Trigger and detection

`runInstall` (internal/cli/install.go) gains an early existing-install check,
before the normal install branches. Detection:

- **Helm path:** `helm list -n kubewise` shows release `kubewise` (or
  `kubectl get deployment -n kubewise kubewise`).
- **Kustomize path:** `kubectl get deployment -n kubewise kubewise-agent`.

If neither exists, behavior is unchanged (fresh install). If one exists, the
release flow runs instead of the normal install.

## Release flow (install exists)

1. **Guard.** Require `--yes` (non-interactive) or an interactive confirmation.
   Before any git mutation, print a preview: the dirty/modified files that will be
   committed and the computed target tag (e.g. `v1.0.3`). Abort on decline.
2. **Commit.** If the working tree is dirty, `git add -A` (respects `.gitignore`)
   and commit with message `chore: release <tag>`. If clean, skip with a note.
3. **Push.** `git push origin main` (or the current branch's upstream).
4. **Tag.** Compute the next patch tag from the latest `v1.0.X` git tag
   (`v1.0.2` → `v1.0.3`). `git tag <tag>` (abort if the tag already exists) and
   `git push origin <tag>`.
5. **Redeploy (best-effort).** `helm upgrade kubewise ./charts/kubewise -n kubewise
   --set image.agent.repository=ghcr.io/lohitkolluri/kubewise-agent
   --set image.agent.tag=1.0.3 --set image.forecaster.tag=1.0.3 --wait
   --timeout 15m`. The `--wait` blocks until the pod is ready, which cannot happen
   until CI has published the `1.0.3` image — so the wait doubles as the poll for
   the CI-built image. Kustomize path: `kubectl set image
   deployment/kubewise-agent agent=ghcr.io/lohitkolluri/kubewise-agent:1.0.3` (and
   forecaster) + `kubectl rollout status --timeout 15m`. If CI is slower than the
   timeout, print guidance: re-run `kwctl install` once the image is published
   (which will detect the newer deployed tag and bump to `v1.0.4`).

## Implementation

New file `internal/cli/install_release.go`:

- `detectInstalled(kc *k8s.Client) (bool, string)` — returns whether an install
  exists and which path (helm/kustomize).
- `latestReleaseTag() string` — parse `git tag` for `^v1\.0\.\d+$`, return the max.
- `nextPatchTag(latest string) string` — `v1.0.X` → `v1.0.(X+1)`.
- `gitRelease(tag string, yes bool) error` — preview + guard, then
  add/commit/push/tag/push-tag, shelling out to `git` (mirrors the existing
  `helm`/`kubectl`/`kind` exec calls in install.go). Returns clear errors on
  auth failure, dirty-tree-with-no-yes, existing tag, or push rejection.
- `redeployWithTag(tag string, path string, out io.Writer) error` — wraps the
  existing `helmInstallWithValues` (Helm) or a `kubectl set image` + rollout
  (kustomize), reusing `installWaitTimeout`.

Wiring: in `runInstall`, after the dev-overlay default block and before the
interactive/non-interactive branch, call `detectInstalled`. If found, run the
release flow and return (skip the normal install). Keep the existing
`--pass`/secret/observability logic out of this path (the install already exists;
only the image changes).

No new dependencies. All git/helm/kubectl operations shell out, consistent with
the rest of install.go.

## Error handling

- **Not on a branch / no upstream:** error telling the user to push first.
- **Tag already exists:** abort with a message (do not force-move tags).
- **Push rejected (non-fast-forward / auth):** abort; surface the git error.
- **Dirty tree without `--yes`:** require confirmation; show the file list.
- **CI image not ready within timeout:** do not fail hard; print re-run guidance.
- **ghcr/Helm unreachable:** the `helm upgrade --wait` error propagates with pod
  diagnostics (reuse `printPodStatus`).

## Testing

- Unit: `nextPatchTag` (`v1.0.2`→`v1.0.3`, `v1.0.9`→`v1.0.10`, empty→`v1.0.2` from
  chart fallback), `latestReleaseTag` parsing, `detectInstalled` logic (mock k8s
  client).
- Integration (manual / e2e): on a kind cluster with an existing install, run
  `kwctl install --yes` with a dirty tree → assert commit + tag `v1.0.3` pushed,
  and the deployment's agent image moves to `:1.0.3` after CI publishes.
- Guard: `kwctl install` with a dirty tree and no `--yes` must prompt and not
  mutate git.

## Out of scope

- Bumping `charts/kubewise/Chart.yaml` version (the `release.yml` `helm` job
  versions the OCI chart independently via `--version`; the live deploy uses the
  local chart).
- A separate `kwctl release` subcommand (this reuses `install` as the entry point
  per the user's request).
- Auto-creating a GitHub Release notes / changelog.

## Resolved decisions

- **Tag format:** `v1.0.3` (repo convention; git tags are `v`-prefixed, image tags
  are stripped to `1.0.3` by the docker job).
- **Scope:** build and tag both agent and forecaster images.
- **Path:** Helm path primary (live `kubewise` deployment); kustomize handled via
  `kubectl set image`.
- **Automatic** on existing-install detection; destructive git ops gated by
  `--yes`/confirm.
