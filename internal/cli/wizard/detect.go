package wizard

import (
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
)

// detectResult is sent back to the bubbletea model after cluster detection.
type detectResult struct {
	ClusterType   string // human-readable description
	Version       string // Kubernetes server version
	NodeCount     int
	HasPrometheus bool
	Error         error
}

// detectClusterCmd runs kubectl probes asynchronously.
func detectClusterCmd() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()

		if _, err := exec.LookPath("kubectl"); err != nil {
			return detectResult{Error: fmt.Errorf("kubectl not found in PATH")}
		}

		// 1. Server version.
		verOut, err := exec.CommandContext(ctx, "kubectl", "version", "-o", "json").Output()
		if err != nil {
			return detectResult{Error: fmt.Errorf("kubectl version failed: %w", err)}
		}
		ver := parseServerVersion(verOut)

		// 2. Node count + provider detection.
		nodeOut, err := exec.CommandContext(ctx, "kubectl", "get", "nodes", "-o", "json").Output()
		if err != nil {
			return detectResult{
				ClusterType: fmt.Sprintf("unknown — kubectl get nodes: %v", err),
				Version:     ver,
			}
		}
		nodes, provider := parseNodes(nodeOut)

		// 3. Check for Prometheus in common namespaces.
		promNS := hasPrometheus(ctx)

		return detectResult{
			ClusterType:   fmt.Sprintf("%s (%s, %d node%s)", provider, ver, nodes, plural(nodes)),
			Version:       ver,
			NodeCount:     nodes,
			HasPrometheus: promNS,
		}
	}
}

// ---- helpers ----

// versionVars is the shape of `kubectl version -o json`.
type versionVars struct {
	ServerVersion struct {
		Major string `json:"major"`
		Minor string `json:"minor"`
	} `json:"serverVersion"`
}

func parseServerVersion(raw []byte) string {
	var vv versionVars
	if json.Unmarshal(raw, &vv) != nil {
		return "unknown"
	}
	mj := strings.TrimSpace(vv.ServerVersion.Major)
	mn := strings.TrimSpace(vv.ServerVersion.Minor)
	if mj == "" || mn == "" {
		return "unknown"
	}
	return fmt.Sprintf("v%s.%s", mj, mn)
}

// nodeItem is a single node from `kubectl get nodes -o json`.
type nodeItem struct {
	Metadata struct {
		Labels map[string]string `json:"labels"`
	} `json:"metadata"`
}

type nodeList struct {
	Items []nodeItem `json:"items"`
}

func parseNodes(raw []byte) (count int, provider string) {
	var nl nodeList
	if json.Unmarshal(raw, &nl) != nil {
		return 0, "unknown"
	}
	count = len(nl.Items)
	if count == 0 {
		return 0, "empty cluster"
	}

	// Collect all labels from all nodes.
	labels := map[string]bool{}
	for _, n := range nl.Items {
		for k := range n.Metadata.Labels {
			labels[k] = true
		}
	}

	provider = detectProvider(labels)
	return count, provider
}

func detectProvider(labels map[string]bool) string {
	// Check order: most specific first.
	for _, p := range providerPatterns {
		for l := range labels {
			if strings.Contains(l, p.label) && (p.value == "" || labels[p.value]) {
				return p.name
			}
		}
	}
	return "Kubernetes"
}

var providerPatterns = []struct {
	label string
	value string
	name  string
}{
	{label: "eks.amazonaws.com/", name: "Amazon EKS"},
	{label: "cloud.google.com/gke-", name: "Google GKE"},
	{label: "kubernetes.azure.com/", name: "Azure AKS"},
	{label: "k3s.io/", name: "K3s"},
	{label: "node-role.kubernetes.io/control-plane", value: "kind.x-k8s.io/role", name: "kind"},
	{label: "minikube.k8s.io/", name: "minikube"},
	{label: "microk8s.io/", name: "MicroK8s"},
}

func hasPrometheus(ctx context.Context) bool {
	ns := []string{"monitoring", "observability", "kubewise", "prometheus"}
	for _, n := range ns {
		//nolint:gosec // CLI tool, intentional kubectl probe
		cmd := exec.CommandContext(ctx, "kubectl", "get", "svc", "-n", n,
			"-o", "name", "--ignore-not-found")
		out, err := cmd.Output()
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(out), "\n") {
			if strings.Contains(line, "prometheus") || strings.Contains(line, "victoria-metrics") {
				return true
			}
		}
	}
	return false
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
