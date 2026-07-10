package cli

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/spf13/cobra"
	corev1 "k8s.io/api/core/v1"
	yaml "gopkg.in/yaml.v3"

	"github.com/lohitkolluri/KubeWise/internal/cli/wizard"
	"github.com/lohitkolluri/KubeWise/internal/version"
	"github.com/lohitkolluri/KubeWise/pkg/k8s"
)

const (
	defaultInstallRef     = "main"
	defaultInstallOverlay = "install"
)

var (
	installYes            bool
	installLocal          bool
	installHelm           bool
	installDryRun         bool
	installChartPath      string
	installRef            string
	installOverlay        string
	installManifestsDir   string
	installOpenRouterKey  string
	installSkipPrometheus bool
	installPrometheusNS   string
	installWaitTimeout    time.Duration
	installObsNamespace   string
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
  kwctl install --helm                   # install via Helm chart (recommended)
  OPENROUTER_API_KEY=sk-... kwctl install   # optional LLM key

After install, port-forward and open the UI:
  kwctl up
  kwctl ui`,
	RunE: runInstall,
}

func init() {
	installCmd.Flags().BoolVar(&installYes, "yes", false, "non-interactive; accept defaults")
	installCmd.Flags().BoolVar(&installLocal, "local", false, "dev mode: kind cluster + build local images")
	installCmd.Flags().BoolVar(&installHelm, "helm", false, "install via Helm chart instead of kustomize manifests")
	installCmd.Flags().BoolVar(&installDryRun, "dry-run", false, "wizard mode: generate config YAML without installing")
	installCmd.Flags().StringVar(&installChartPath, "chart", "", "Helm chart path or URL (default: GitHub chart at --ref)")
	installCmd.Flags().StringVar(&installRef, "ref", defaultInstallRef, "git ref for remote kustomize overlay or Helm chart")
	installCmd.Flags().StringVar(&installOverlay, "overlay", defaultInstallOverlay, "kustomize overlay name (install, dev, prod)")
	installCmd.Flags().StringVar(&installManifestsDir, "manifests-dir", "", "local manifests path (skips remote kustomize)")
	installCmd.Flags().StringVar(&installOpenRouterKey, "openrouter-key", "", "OpenRouter API key (or set OPENROUTER_API_KEY)")
	installCmd.Flags().BoolVar(&installSkipPrometheus, "skip-prometheus", false, "skip Prometheus auto-detection")
	installCmd.Flags().StringVar(&installPrometheusNS, "prometheus-namespace", "monitoring", "namespace to search for Prometheus")
	installCmd.Flags().DurationVar(&installWaitTimeout, "wait", 10*time.Minute, "timeout waiting for agent rollout (first install may need 5-10m to pull images)")
	installCmd.Flags().StringVar(&installObsNamespace, "observability-namespace", "", "namespace to probe for existing observability backends (default: monitoring)")
	rootCmd.AddCommand(installCmd)
}

func runInstall(cmd *cobra.Command, _ []string) error {
	out := cmd.OutOrStdout()

	if installLocal {
		return runLocalInstall(out)
	}

	// Dev builds (running from source) should default to local manifests + dev overlay,
	// so "kwctl install" doesn't accidentally apply remote release overlays.
	if version.Version == "dev" &&
		installRef == defaultInstallRef &&
		installOverlay == defaultInstallOverlay &&
		installManifestsDir == "" {
		if local := findManifestsDir(); local != "" {
			installManifestsDir = local
			installOverlay = "dev"
		}
	}

	// Interactive wizard mode — no flags set.
	nonInteractive := installYes || installHelm || installChartPath != "" ||
		installManifestsDir != "" || installOverlay != defaultInstallOverlay
	if !nonInteractive || installDryRun {
		return runInstallWizard(out)
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
	printOK(out, "Kubernetes cluster reachable")

	if installHelm {
		if err := runHelmInstallWithObservability(ctx, out, kc); err != nil {
			printPodStatus(out, kc, agentNS)
			return err
		}
	} else {
		if err := applyInstallManifests(out); err != nil {
			return err
		}
		printOK(out, "Manifests applied")

		if err := applyInstallSecret(); err != nil {
			return err
		}

		if !installSkipPrometheus {
			if be := kc.DetectMetricsBackend(ctx, []string{installPrometheusNS}); be != nil {
				if err := kc.PatchConfigMapPrometheus(ctx, agentNS, be.URL); err != nil {
					printWarn(out, "could not patch Prometheus URL: %v", err)
				} else {
					printOK(out, "Prometheus detected: %s", be.URL)
				}
			} else if !installYes {
				printWarn(out, "no Prometheus found in namespace %q — metrics collection may fail until configured", installPrometheusNS)
				printHint(out, "install Prometheus or run: kwctl config set prometheus_address http://...")
			}
		}
	}

	if err := runWithSpinner(out, "Waiting for agent deployment to become ready...", func() error {
		waitCtx, waitCancel := context.WithTimeout(ctx, installWaitTimeout)
		defer waitCancel()
		return waitForAnyAgentDeployment(waitCtx, kc, agentNS, agentSvc)
	}); err != nil {
		printPodStatus(out, kc, agentNS)
		return fmt.Errorf("agent not ready: %w", err)
	}
	printOK(out, "Agent is running")

	if err := saveInstallProfile(); err != nil {
		printWarn(out, "could not save kwctl profile: %v", err)
	} else {
		printOK(out, "kwctl profile saved (~/.config/kwctl/config.yaml)")
	}

	printInstallNextSteps(out)
	return nil
}

// ---------------------------------------------------------------------------
// Observability auto-detection + install orchestration
// ---------------------------------------------------------------------------

// observabilityHelmConfig holds the computed install configuration.
type observabilityHelmConfig struct {
	metricsURL   string
	logsEndpoint string
	logsPushURL  string
	tracesURL    string
	tracesOTLP   string
}

// runHelmInstallWithObservability detects backends, then installs KubeWise (subcharts
// handle missing backends automatically).
func runHelmInstallWithObservability(ctx context.Context, out io.Writer, kc *k8s.Client) error {
	printSection(out, "Observability")

	report := kc.DetectAll(ctx, installObsNamespace)
	obsCfg := observabilityHelmConfig{}

	if report.Metrics != nil {
		obsCfg.metricsURL = report.Metrics.URL
		printOK(out, "Metrics: %s (%s)", report.Metrics.Type, report.Metrics.URL)
	} else {
		printWarn(out, "No metrics backend detected — VM subchart will install VictoriaMetrics")
	}

	if report.Logs != nil {
		obsCfg.logsEndpoint = report.Logs.URL
		obsCfg.logsPushURL = report.Logs.PushURL
		printOK(out, "Logs: %s (%s)", report.Logs.Type, report.Logs.URL)
	} else {
		printWarn(out, "No logs backend detected — VL subchart will install VictoriaLogs")
	}

	if report.Traces != nil {
		obsCfg.tracesURL = report.Traces.URL
		obsCfg.tracesOTLP = report.Traces.OTLPEndpoint
		printOK(out, "Traces: %s (%s)", report.Traces.Type, report.Traces.URL)
	} else {
		printWarn(out, "No traces backend detected — Tempo subchart will install")
	}

	printSection(out, "KubeWise Install")
	if err := applyHelmInstallWithObservability(out, obsCfg); err != nil {
		return err
	}
	printOK(out, "Helm release installed")
	return nil
}

// applyHelmInstallWithObservability installs the KubeWise chart with auto-detected URLs.
// It disables subcharts for backends that were detected externally, and enables them for
// backends that need to be installed.
func applyHelmInstallWithObservability(out io.Writer, obsCfg observabilityHelmConfig) error {
	if err := requireHelm(); err != nil {
		return err
	}
	if err := requireKubectl(); err != nil {
		return err
	}

	chart := installChartPath
	if chart == "" {
		if local := findHelmChartDir(); local != "" {
			chart = local
		} else {
			chart = fmt.Sprintf("https://github.com/lohitkolluri/KubeWise/charts/kubewise?ref=%s", installRef)
		}
	}

	// Run dep update for local charts to fetch VM+VL subchart dependencies
	if local := findHelmChartDir(); local != "" {
		_ = runHelmDepUpdate(local)
	}

	_, _ = fmt.Fprintf(out, "Helm install: %s (namespace %s)\n", chart, agentNS)

	key := installOpenRouterKey
	if key == "" {
		key = os.Getenv("OPENROUTER_API_KEY")
	}
	apiTok := os.Getenv("KUBEWISE_API_TOKEN")
	requireToken := os.Getenv("KUBEWISE_REQUIRE_API_TOKEN") == "true"

	// Subchart toggles: if we have an explicit override URL, disable the subchart.
	// If no override URL, enable the subchart to install the backend.
	vmSubchart := obsCfg.metricsURL == ""
	vlSubchart := obsCfg.logsEndpoint == ""
	tempoSubchart := obsCfg.tracesURL == ""

	alloyEnabled := obsCfg.logsEndpoint != "" || vlSubchart

	// Build Helm values
	values := map[string]any{
		"agent": map[string]any{
			"prometheusAddress": obsCfg.metricsURL,
			"features": map[string]any{
				"observability": true,
			},
			"observability": map[string]any{
				"metricsURL":         obsCfg.metricsURL,
				"logsEndpoint":       obsCfg.logsEndpoint,
				"logsPushEndpoint":   obsCfg.logsPushURL,
				"tracesEndpoint":     obsCfg.tracesURL,
				"tracesOTLPEndpoint": obsCfg.tracesOTLP,
				"vm": map[string]any{
					"enabled": vmSubchart,
				},
				"vl": map[string]any{
					"enabled": vlSubchart,
				},
				"tempo": map[string]any{
					"enabled": tempoSubchart,
				},
				"alloy": map[string]any{
					"enabled": alloyEnabled,
				},
			},
		},
	}

	if key != "" || apiTok != "" {
		secrets := map[string]any{}
		if key != "" {
			secrets["openrouterApiKey"] = key
		}
		if apiTok != "" {
			secrets["apiToken"] = apiTok
		}
		values["secrets"] = secrets
	}
	if requireToken {
		values["security"] = map[string]any{"requireApiToken": true}
	}

	return helmInstallWithValues(out, chart, values)
}

// runHelmDepUpdate runs helm dep update on a local chart directory.
func runHelmDepUpdate(chartDir string) error {
	cmd := exec.Command("helm", "dep", "update", chartDir) //nolint:gosec // CLI tool, intentional helm dep update
	return cmd.Run()
}

// helmInstallWithValues writes values to a temp file and runs helm upgrade --install.
// Output is suppressed (shown via spinner) and the captured stderr is returned on error
// so the caller can display it alongside pod diagnostics.
func helmInstallWithValues(out io.Writer, chart string, values map[string]any) error {
	args := []string{
		"upgrade", "--install", "kubewise", chart,
		"-n", agentNS, "--create-namespace",
		"--wait", "--timeout", installWaitTimeout.String(),
	}

	tmpPath := ""
	if len(values) > 0 {
		b, err := yaml.Marshal(values)
		if err != nil {
			return fmt.Errorf("marshal helm values: %w", err)
		}
		f, err := os.CreateTemp("", "kubewise-values-*.yaml")
		if err != nil {
			return fmt.Errorf("create temp values file: %w", err)
		}
		tmpPath = f.Name()
		_ = f.Chmod(0o600)
		if _, err := f.Write(b); err != nil {
			_ = f.Close()
			_ = os.Remove(tmpPath)
			return fmt.Errorf("write temp values file: %w", err)
		}
		_ = f.Close()
		defer func() { _ = os.Remove(tmpPath) }()
		args = append(args, "-f", tmpPath)
	}

	return runWithSpinner(out, "Installing Helm chart — pulling images and waiting for resources...", func() error {
		c := exec.Command("helm", args...) //nolint:gosec // CLI tool, intentional helm install
		// Suppress helm's own progress output during the spinner; capture stderr
		// so we can show it on failure.
		c.Stdout = io.Discard
		var stderrBuf bytes.Buffer
		c.Stderr = &stderrBuf
		if err := c.Run(); err != nil {
			// Print captured helm output before returning the error.
			if stderrBuf.Len() > 0 {
				_, _ = fmt.Fprintln(out, mutedStyle.Render(stderrBuf.String()))
			}
			return fmt.Errorf("helm install failed: %w", err)
		}
		return nil
	})
}

func waitForAnyAgentDeployment(ctx context.Context, kc *k8s.Client, namespace, preferred string) error {
	candidates := []string{
		preferred,
		"kubewise",
		"kubewise-agent",
	}
	seen := map[string]bool{}
	var lastErr error
	for _, name := range candidates {
		name = strings.TrimSpace(name)
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		if err := kc.WaitForDeploymentAvailable(ctx, namespace, name); err != nil {
			lastErr = err
		} else {
			return nil
		}
	}
	if lastErr != nil {
		return lastErr
	}
	return fmt.Errorf("deployment not found")
}

// ---------------------------------------------------------------------------
// Spinner progress helpers
// ---------------------------------------------------------------------------

// spinnerFrames is a simple braille spinner for CLI progress indication.
var spinnerFrames = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}

// runWithSpinner shows an animated spinner while fn executes. It is a
// no-op (runs fn directly) when w is not a terminal.
func runWithSpinner(w io.Writer, msg string, fn func() error) error {
	if !writerTTY(w) {
		return fn()
	}
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		i := 0
		for {
			select {
			case <-done:
				_, _ = fmt.Fprintf(w, "\r%s\r", strings.Repeat(" ", ansiWidth(msg)+4))
				return
			default:
				_, _ = fmt.Fprintf(w, "\r%s %s", infoStyle.Render(spinnerFrames[i]), mutedStyle.Render(msg))
			}
			i = (i + 1) % len(spinnerFrames)
			time.Sleep(120 * time.Millisecond)
		}
	}()
	err := fn()
	close(done)
	wg.Wait()
	return err
}

// ansiWidth returns the visible width of a string, ignoring ANSI escape sequences.
func ansiWidth(s string) int {
	const esc = '\x1b'
	w := 0
	in := false
	for _, r := range s {
		if r == esc {
			in = true
			continue
		}
		if in {
			if r == 'm' {
				in = false
			}
			continue
		}
		if r >= ' ' && r < 0x7f || r > 0x9f {
			w++
		}
	}
	return w
}

// printPodStatus prints a compact table of pods and their statuses in the given
// namespace. It is used to provide actionable diagnostics after a failed install.
func printPodStatus(w io.Writer, kc *k8s.Client, namespace string) {
	pods, err := kc.GetPods(context.Background(), namespace)
	if err != nil {
		return
	}
	_, _ = fmt.Fprintln(w, mutedStyle.Render("\nPod status in namespace "+namespace+":"))
	for _, p := range pods.Items {
		status, detail := compactPodStatus(&p)
		var line string
		switch {
		case p.Status.Phase == corev1.PodRunning && p.Status.ContainerStatuses[0].Ready:
			line = successStyle.Render("● " + status) + " " + mutedStyle.Render(p.Name)
		case p.Status.Phase == corev1.PodPending:
			line = warnStyle.Render("● " + status) + " " + mutedStyle.Render(p.Name)
			if detail != "" {
				line += "\n" + mutedStyle.Render("  └ " + detail)
			}
		default:
			line = errStyle.Render("● " + status) + " " + mutedStyle.Render(p.Name)
			if detail != "" {
				line += "\n" + mutedStyle.Render("  └ " + detail)
			}
		}
		_, _ = fmt.Fprintln(w, line)
	}
}

// compactPodStatus returns a short status label and optional detail for a pod.
func compactPodStatus(p *corev1.Pod) (status, detail string) {
	switch p.Status.Phase {
	case corev1.PodRunning:
		ready, total := 0, len(p.Status.ContainerStatuses)
		for _, cs := range p.Status.ContainerStatuses {
			if cs.Ready {
				ready++
			}
		}
		return fmt.Sprintf("Running (%d/%d)", ready, total), ""
	case corev1.PodPending:
		// Check container waiting reasons (image pull, etc.)
		for _, cs := range p.Status.ContainerStatuses {
			if cs.State.Waiting != nil {
				reason := cs.State.Waiting.Reason
				if reason == "" {
					reason = "waiting"
				}
				msg := cs.State.Waiting.Message
				if msg == "" {
					msg = "container " + cs.Name
				}
				return "Pending", reason + ": " + msg
			}
		}
		return "Pending", "scheduling"
	case corev1.PodSucceeded:
		return "Succeeded", ""
	case corev1.PodFailed:
		return "Failed", ""
	default:
		return string(p.Status.Phase), ""
	}
}

func runInstallWizard(out io.Writer) error {
	m := wizard.New()
	if installDryRun {
		m.State().DryRun = true
	}

	p := tea.NewProgram(m)
	model, err := p.Run()
	if err != nil {
		return fmt.Errorf("wizard error: %w", err)
	}

	finalModel := model.(wizard.Model)
	if !finalModel.IsComplete() {
		_, _ = fmt.Fprintln(out, "Install cancelled.")
		return nil
	}

	result, err := finalModel.State().Execute(context.Background())
	if result != "" {
		_, _ = fmt.Fprintln(out, result)
	}
	if err != nil {
		return fmt.Errorf("install: %w", err)
	}

	if !installDryRun {
		_, _ = fmt.Fprintln(out, "\nKubeWise is installed.")
	}
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
	c := exec.Command("bash", script, "--local", "--yes") //nolint:gosec // CLI tool, intentional local install
	c.Stdout = out
	c.Stderr = os.Stderr
	c.Env = os.Environ()
	if err := c.Run(); err != nil {
		return err
	}
	if err := saveInstallProfile(); err != nil {
		_, _ = fmt.Fprintf(out, "warning: could not save kwctl profile: %v\n", err)
	}
	printInstallNextSteps(out)
	return nil
}

func findBootstrapScript() string {
	if repo := os.Getenv("KUBEWISE_REPO"); repo != "" {
		p := repo + "/hack/bootstrap.sh"
		if _, err := os.Stat(p); err == nil { //nolint:gosec // CLI tool, bootstrap script detection
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

func requireHelm() error {
	if _, err := exec.LookPath("helm"); err != nil {
		return fmt.Errorf("helm not found in PATH — install Helm or use kwctl install without --helm")
	}
	return nil
}

func findHelmChartDir() string {
	candidates := []string{}
	if repo := os.Getenv("KUBEWISE_REPO"); repo != "" {
		candidates = append(candidates, repo+"/charts/kubewise")
	}
	if wd, err := os.Getwd(); err == nil {
		candidates = append(candidates, wd+"/charts/kubewise")
	}
	for _, p := range candidates {
		if _, err := os.Stat(p + "/Chart.yaml"); err == nil { //nolint:gosec // CLI tool, local chart detection
			return p
		}
	}
	return ""
}

func findManifestsDir() string {
	candidates := []string{}
	if repo := os.Getenv("KUBEWISE_REPO"); repo != "" {
		candidates = append(candidates, repo+"/manifests")
	}
	if wd, err := os.Getwd(); err == nil {
		candidates = append(candidates, wd+"/manifests")
	}
	for _, p := range candidates {
		if _, err := os.Stat(p + "/overlays/dev/kustomization.yaml"); err == nil { //nolint:gosec // CLI tool, local manifests detection
			return p
		}
	}
	return ""
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
	_, _ = fmt.Fprintf(out, "Applying: %s\n", target)
	c := exec.Command("kubectl", "apply", "-k", target) //nolint:gosec // CLI tool, intentional kubectl apply
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
			_, _ = fmt.Fprintln(os.Stdout, "ℹ No OPENROUTER_API_KEY — running in observe-only mode (dry-run remediation)")
		}
		return nil
	}

	args := []string{
		"create", "secret", "generic", "kubewise-secret",
		"-n", agentNS,
		"--dry-run=client", "-o", "yaml",
	}
	if key != "" {
		args = append(args, "--from-literal=openrouter_api_key="+key)
	}
	if apiTok != "" {
		args = append(args, "--from-literal=api_token="+apiTok)
	}
	create := exec.Command("kubectl", args...) //nolint:gosec // CLI tool, intentional kubectl create secret
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
	_, _ = fmt.Fprintln(os.Stdout, "✓ Secret applied")
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
	printOK(out, "KubeWise is installed")
	printNextSteps(out,
		"kwctl up        # connect to the agent",
		"kwctl ui        # open control center",
		"kwctl connect   # verify connectivity",
	)
}
