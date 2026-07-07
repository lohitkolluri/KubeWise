// Package version is the single source of truth for release metadata.
package version

// Set at link time via -ldflags; defaults for dev builds.
var (
	Version = "dev"
	Commit  = "none"
)

// AgentName is the in-cluster HTTP service identifier.
const AgentName = "kubewise-agent"
