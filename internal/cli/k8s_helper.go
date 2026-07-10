package cli

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/k8s"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func newKubeClient() (*k8s.Client, error) {
	return k8s.NewFromKubeconfigContext(kubeconfig, contextName)
}

func agentDeploymentCandidates() []string {
	candidates := []string{
		"kubewise",
		"kubewise-agent",
		agentSvc,
		strings.TrimSuffix(agentSvc, "-agent"),
		strings.TrimSuffix(agentSvc, "-service"),
	}
	seen := map[string]bool{}
	var out []string
	for _, name := range candidates {
		name = strings.TrimSpace(name)
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		out = append(out, name)
	}
	return out
}

func findAgentPod(ctx context.Context) (podName string, err error) {
	kc, err := newKubeClient()
	if err != nil {
		return "", err
	}
	pod, err := kc.FindRunningAgentPod(ctx, agentNS, agentDeploymentCandidates())
	if err != nil {
		return "", err
	}
	return pod.Name, nil
}

func fetchAgentLogs(tail int64) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	kc, err := newKubeClient()
	if err != nil {
		return "", err
	}
	podName, err := findAgentPod(ctx)
	if err != nil {
		return "", err
	}
	container := logsContainer
	if container == "" {
		container = "agent"
	}
	return kc.GetPodLogs(ctx, agentNS, podName, container, tail)
}

func streamAgentLogs(ctx context.Context, tail int64, fn func(string)) error {
	kc, err := newKubeClient()
	if err != nil {
		return err
	}
	podName, err := findAgentPod(ctx)
	if err != nil {
		return err
	}
	container := logsContainer
	if container == "" {
		container = "agent"
	}
	return kc.StreamPodLogs(ctx, agentNS, podName, container, tail, fn)
}

func restartAgentDeployment() error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	kc, err := newKubeClient()
	if err != nil {
		return err
	}
	var lastErr error
	for _, depName := range agentDeploymentCandidates() {
		if err := kc.RolloutRestart(ctx, agentNS, depName); err != nil {
			lastErr = err
		} else {
			return nil
		}
	}
	if lastErr != nil {
		return fmt.Errorf("restart agent deployment: %w", lastErr)
	}
	return fmt.Errorf("restart agent deployment: not found")
}

func formatConfigSummary(cfg *models.AgentConfig) string {
	if cfg == nil {
		return "(no config)"
	}
	var b strings.Builder
	fmt.Fprintf(&b, "Scrape:      %s\n", cfg.ScrapeInterval)
	fmt.Fprintf(&b, "Prometheus:  %s\n", cfg.PrometheusAddress)
	fmt.Fprintf(&b, "LLM:         %s / %s\n", cfg.LLMProvider, cfg.LLMModel)
	fmt.Fprintf(&b, "Remediation: %s\n", cfg.Remediation.Mode)
	fmt.Fprintf(&b, "Dry run:     %v\n", cfg.Remediation.DryRun)
	if cfg.Remediation.RateLimit > 0 {
		fmt.Fprintf(&b, "Rate limit:  %d\n", cfg.Remediation.RateLimit)
	}
	if cfg.Remediation.MinConfidence > 0 {
		fmt.Fprintf(&b, "Min conf:    %.2f\n", cfg.Remediation.MinConfidence)
	}
	return b.String()
}
