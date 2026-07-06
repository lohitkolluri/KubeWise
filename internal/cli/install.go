package cli

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

const (
	defaultInstallRef     = "main"
	defaultInstallOverlay = "install"
)

var (
	installYes            bool
	installLocal          bool
	installRef            string
	installOverlay        string
	installManifestsDir   string
	installOpenRouterKey  string
	installSkipPrometheus bool
	installPrometheusNS   string
	installWaitTimeout    time.Duration
)

var installCmd = &cobra.Command{
	Use:   "install",
	Short: "Install KubeWise into your Kubernetes cluster (one command)",
	Long: `Install the KubeWise agent with minimal setup.

Requires: kubectl, a working kubeconfig, and cluster admin (for RBAC).

Examples:
  kwctl install                          # apply remote manifests, detect Prometheus
  kwctl install --yes                    # non-interactive
  kwctl install --local                  # kind + build images (dev laptop)
  OPENROUTER_API_KEY=sk-... kwctl install   # optional LLM key

After install, port-forward and open the UI:
  kubectl -n kubewise port-forward svc/kubewise-agent 8080:8080
  kwctl ui`,
	RunE: runInstall,
}

func init() {
	installCmd.Flags().BoolVar(&installYes, "yes", false, "non-interactive; accept defaults")
	installCmd.Flags().BoolVar(&installLocal, "local", false, "dev mode: kind cluster + build local images")
	installCmd.Flags().StringVar(&installRef, "ref", defaultInstallRef, "git ref for remote kustomize overlay")
	installCmd.Flags().StringVar(&installOverlay, "overlay", defaultInstallOverlay, "kustomize overlay name (install, dev, prod)")
	installCmd.Flags().StringVar(&installManifestsDir, "manifests-dir", "", "local manifests path (skips remote kustomize)")
	installCmd.Flags().StringVar(&installOpenRouterKey, "openrouter-key", "", "OpenRouter API key (or set OPENROUTER_API_KEY)")
	installCmd.Flags().BoolVar(&installSkipPrometheus, "skip-prometheus", false, "skip Prometheus auto-detection")
	installCmd.Flags().StringVar(&installPrometheusNS, "prometheus-namespace", "monitoring", "namespace to search for Prometheus")
	installCmd.Flags().DurationVar(&installWaitTimeout, "wait", 3*time.Minute, "timeout waiting for agent rollout")
	rootCmd.AddCommand(installCmd)
}

func runInstall(cmd *cobra.Command, _ []string) error {
	out := cmd.OutOrStdout()

	if installLocal {
		return runLocalInstall(out)
	}

	if err := requireKubectl(); err != nil {
		return err
	}

	kc, err := newKubeClient()
	if err != nil {
		return fmt.Errorf("kubeconfig: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), installWaitTimeout+30*time.Second)
	defer cancel()
	if err := kc.PingCluster(ctx); err != nil {
		return err
	}
	fmt.Fprintln(out, "✓ Kubernetes cluster reachable")

	if err := applyInstallManifests(out); err != nil {
		return err
	}
	fmt.Fprintln(out, "✓ Manifests applied")

	if err := applyInstallSecret(); err != nil {
		return err
	}

	if !installSkipPrometheus {
		if url, ok := kc.DetectPrometheusURL(ctx, installPrometheusNS); ok {
			if err := kc.PatchConfigMapPrometheus(ctx, agentNS, url); err != nil {
				fmt.Fprintf(out, "warning: could not patch Prometheus URL: %v\n", err)
			} else {
				fmt.Fprintf(out, "✓ Prometheus detected: %s\n", url)
			}
		} else if !installYes {
			fmt.Fprintf(out, "warning: no Prometheus found in namespace %q — metrics collection may fail until configured\n", installPrometheusNS)
			fmt.Fprintln(out, "  install Prometheus or run: kwctl config set prometheus_address http://...")
		}
	}

	fmt.Fprintln(out, "… waiting for agent deployment")
	waitCtx, waitCancel := context.WithTimeout(ctx, installWaitTimeout)
	defer waitCancel()
	if err := kc.WaitForDeploymentAvailable(waitCtx, agentNS, agentSvc); err != nil {
		return fmt.Errorf("agent not ready: %w", err)
	}
	fmt.Fprintln(out, "✓ Agent is running")

	if err := saveInstallProfile(); err != nil {
		fmt.Fprintf(out, "warning: could not save kwctl profile: %v\n", err)
	} else {
		fmt.Fprintln(out, "✓ kwctl profile saved (~/.config/kwctl/config.yaml)")
	}

	printInstallNextSteps(out)
	return nil
}

func runLocalInstall(out io.Writer) error {
	if _, err := exec.LookPath("bash"); err != nil {
		return fmt.Errorf("bash required for --local install")
	}
	script := findBootstrapScript()
	if script == "" {
		return fmt.Errorf("hack/bootstrap.sh not found — run from repo root or set KUBEWISE_REPO")
	}
	c := exec.Command("bash", script, "--local", "--yes")
	c.Stdout = out
	c.Stderr = os.Stderr
	c.Env = os.Environ()
	return c.Run()
}

func findBootstrapScript() string {
	if repo := os.Getenv("KUBEWISE_REPO"); repo != "" {
		p := repo + "/hack/bootstrap.sh"
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	if wd, err := os.Getwd(); err == nil {
		p := wd + "/hack/bootstrap.sh"
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

func requireKubectl() error {
	if _, err := exec.LookPath("kubectl"); err != nil {
		return fmt.Errorf("kubectl not found in PATH — install kubectl first")
	}
	return nil
}

func applyInstallManifests(out io.Writer) error {
	var target string
	if installManifestsDir != "" {
		target = installManifestsDir
		if installOverlay != "" {
			target = strings.TrimRight(installManifestsDir, "/") + "/overlays/" + installOverlay
		}
	} else {
		target = fmt.Sprintf("github.com/lohitkolluri/KubeWise/manifests/overlays/%s?ref=%s", installOverlay, installRef)
	}
	fmt.Fprintf(out, "Applying: %s\n", target)
	c := exec.Command("kubectl", "apply", "-k", target)
	c.Stdout = out
	c.Stderr = os.Stderr
	return c.Run()
}

func applyInstallSecret() error {
	key := installOpenRouterKey
	if key == "" {
		key = os.Getenv("OPENROUTER_API_KEY")
	}
	apiTok := os.Getenv("KUBEWISE_API_TOKEN")

	if key == "" && apiTok == "" {
		if !installYes {
			fmt.Fprintln(os.Stdout, "ℹ No OPENROUTER_API_KEY — running in observe-only mode (dry-run remediation)")
		}
		return nil
	}

	args := []string{
		"create", "secret", "generic", "kubewise-agent-secret",
		"-n", agentNS,
		"--dry-run=client", "-o", "yaml",
	}
	if key != "" {
		args = append(args, "--from-literal=openrouter_api_key="+key)
	}
	if apiTok != "" {
		args = append(args, "--from-literal=api_token="+apiTok)
	}
	create := exec.Command("kubectl", args...)
	yaml, err := create.Output()
	if err != nil {
		return fmt.Errorf("create secret manifest: %w", err)
	}
	apply := exec.Command("kubectl", "apply", "-f", "-")
	apply.Stdin = strings.NewReader(string(yaml))
	apply.Stdout = os.Stdout
	apply.Stderr = os.Stderr
	if err := apply.Run(); err != nil {
		return fmt.Errorf("apply secret: %w", err)
	}
	fmt.Fprintln(os.Stdout, "✓ Secret applied")
	return nil
}

func saveInstallProfile() error {
	pf, err := loadProfileFile()
	if err != nil {
		return err
	}
	prof := pf.Profiles[pf.Current]
	if prof.AgentURL == "" {
		prof = defaultProfileFile().Profiles["default"]
	}
	prof.AgentURL = defaultAgentURL
	prof.AgentNamespace = agentNS
	prof.AgentService = agentSvc
	if prof.Output == "" {
		prof.Output = "table"
	}
	if prof.TimeoutSeconds <= 0 {
		prof.TimeoutSeconds = 15
	}
	if tok := os.Getenv("KUBEWISE_API_TOKEN"); tok != "" {
		prof.APIToken = tok
	}
	if kubeconfig != "" {
		prof.Kubeconfig = kubeconfig
	}
	if contextName != "" {
		prof.Context = contextName
	}
	pf.Profiles[pf.Current] = prof
	return saveProfileFile(pf)
}

func printInstallNextSteps(out io.Writer) {
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "KubeWise is installed.")
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "  1. Port-forward the agent API:")
	fmt.Fprintf(out, "     kubectl -n %s port-forward svc/%s 8080:8080\n", agentNS, agentSvc)
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "  2. Open the control center:")
	fmt.Fprintln(out, "     kwctl ui")
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "  Optional: set LLM key later with OPENROUTER_API_KEY or kwctl config")
}
