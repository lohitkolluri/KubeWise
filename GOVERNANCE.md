# Governance

KubeWise is a small, early-stage open source project. This document describes how it is run **today** — not an aspirational future state.

## Leadership model

**Maintainer-led.** One primary maintainer merges changes and cuts releases. External contributors are welcome via pull requests.

## Roles

| Role            | Who                                  | Responsibilities                                               |
| --------------- | ------------------------------------ | -------------------------------------------------------------- |
| **Maintainer**  | See [MAINTAINERS.md](MAINTAINERS.md) | Review/merge PRs, releases, roadmap direction                  |
| **Contributor** | Anyone                               | Open issues and PRs; follow [CONTRIBUTING.md](CONTRIBUTING.md) |

There is no steering committee or elections at this stage.

## Decision making

- **Day-to-day:** Maintainer merges PRs after CI passes and a reasonable review.
- **Technical direction:** Discussed in GitHub issues; maintainer has final call while the project is small.
- **Breaking changes:** Announced in release notes; semver tags on releases.

## Releases

- Tagged `v*` on `main` triggers [GoReleaser](.goreleaser.yaml) (binaries + npm).
- Chart and install image tags are bumped manually — see [CONTRIBUTING.md](CONTRIBUTING.md#releases).

## Scope

**In scope:** In-cluster Kubernetes predictive observability, safe remediation with human approval, `kwctl` CLI.

**Out of scope (for now):** Hosted SaaS, multi-tenant control plane, vendor-specific forks of core agent logic.

## Evolving governance

As adoption grows, we may add reviewers, a second maintainer from another organization, or formal contributor promotion. Changes to this file will be proposed via PR.
