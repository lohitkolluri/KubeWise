## Migration guide

KubeWise is still early-stage and most upgrades are backward compatible.

### If you upgrade `kwctl`
- `kwctl` is safe to upgrade independently of the in-cluster agent.

### If you upgrade the in-cluster agent
- **Helm users**: upgrade with `helm upgrade` and keep your values file.
- **Manifests users**: re-apply the latest overlay and restart the deployment.

### Feature flags
Most “v2” capabilities are **feature-gated** and default to **off**. If you enable flags, do it one at a time and validate:
- `kwctl connect`
- `kwctl status`
- `kwctl logs -f`

### Breaking changes policy
Breaking changes are communicated in GitHub Release notes. Tagged `vX.Y.Z` releases follow semver conventions.

