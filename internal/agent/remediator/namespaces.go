package remediator

// builtInProtectedNamespaces are always blocked from automated remediation.
// Config Denylist adds operator-specific namespaces on top of this set.
var builtInProtectedNamespaces = map[string]bool{
	"kube-system":       true,
	"kube-public":       true,
	"kube-node-lease":   true,
	"istio-system":      true,
	"cert-manager":      true,
	"gatekeeper-system": true,
	"velero":            true,
	"metallb-system":    true,
	"ingress-nginx":     true,
	"linkerd":           true,
	"linkerd-viz":       true,
}

func isBuiltInProtectedNamespace(namespace string) bool {
	return builtInProtectedNamespaces[namespace]
}
