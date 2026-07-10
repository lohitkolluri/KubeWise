package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"golang.org/x/term"
)

func init() {
	secretsCmd.AddCommand(secretsSetOpenRouterKeyCmd, secretsSetAPITokenCmd)
	rootCmd.AddCommand(secretsCmd)

	secretsSetOpenRouterKeyCmd.Flags().BoolVar(&secretsFromStdin, "stdin", false, "read key from stdin (recommended to avoid shell history)")
	secretsSetOpenRouterKeyCmd.Flags().StringVar(&secretsFromEnv, "from-env", "", "read key from an environment variable (e.g. OPENROUTER_API_KEY)")
	secretsSetOpenRouterKeyCmd.Flags().BoolVar(&secretsRestartAgent, "restart", true, "restart agent deployment after updating secret")

	secretsSetAPITokenCmd.Flags().BoolVar(&secretsAPITokenFromStdin, "stdin", false, "read token from stdin (recommended to avoid shell history)")
	secretsSetAPITokenCmd.Flags().StringVar(&secretsAPITokenFromEnv, "from-env", "", "read token from an environment variable (e.g. KUBEWISE_API_TOKEN)")
	secretsSetAPITokenCmd.Flags().BoolVar(&secretsAPITokenRestartAgent, "restart", true, "restart agent deployment after updating secret")
}

var secretsCmd = &cobra.Command{
	Use:   "secrets",
	Short: "Manage agent secrets (API keys, tokens)",
}

var (
	secretsFromStdin    bool
	secretsFromEnv      string
	secretsRestartAgent bool
)

var (
	secretsAPITokenFromStdin    bool
	secretsAPITokenFromEnv      string
	secretsAPITokenRestartAgent bool
)

var secretsSetOpenRouterKeyCmd = &cobra.Command{
	Use:   "set-openrouter-key [KEY]",
	Short: "Set OpenRouter API key in the cluster secret",
	Long: `Sets the OpenRouter API key in the Kubernetes secret referenced by the running agent deployment.

Examples:
  kwctl secrets set-openrouter-key --stdin
  kwctl secrets set-openrouter-key --from-env OPENROUTER_API_KEY
  kwctl secrets set-openrouter-key sk-or-...`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		if secretsFromStdin && len(args) == 1 {
			return fmt.Errorf("got KEY argument while --stdin is set; use either --stdin (paste key via stdin) or pass KEY as an argument, not both")
		}

		key, err := readSecretInput(cmd, args)
		if err != nil {
			return err
		}
		key = strings.TrimSpace(key)
		if key == "" {
			return fmt.Errorf("key must not be empty")
		}
		if err := validateOpenRouterKey(key); err != nil {
			return err
		}

		secName, err := resolveAgentSecretName(cmd.Context(), "OPENROUTER_API_KEY")
		if err != nil {
			return err
		}
		if secName == "" {
			// Fallback to conventional name used by manifests.
			secName = "kubewise-secret"
		}

		if err := upsertSecretStringData(cmd.Context(), agentNS, secName, map[string]string{
			"openrouter_api_key": key,
		}); err != nil {
			return err
		}

		printOK(cmd.OutOrStdout(), "OpenRouter key saved in secret %s/%s", agentNS, secName)
		printNextSteps(cmd.OutOrStdout(), "kwctl up", "kwctl ui", "kwctl status")
		if secretsRestartAgent {
			depName, _ := resolveAgentDeploymentName(cmd.Context())
			if depName == "" {
				depName = "kubewise-agent"
			}
			if err := rolloutRestartDeployment(agentNS, depName); err != nil {
				return err
			}
			printOK(cmd.OutOrStdout(), "Agent restarted")
		} else {
			printHint(cmd.OutOrStdout(), "restart agent to apply: kubectl -n kubewise rollout restart deploy/<agent-deployment>")
		}
		return nil
	},
}

var secretsSetAPITokenCmd = &cobra.Command{
	Use:   "set-api-token [TOKEN]",
	Short: "Set agent API token in the cluster secret",
	Long: `Sets the API token used by the agent HTTP API in the Kubernetes secret referenced by the running agent deployment.

This keeps the agent-side token (KUBEWISE_API_TOKEN) aligned with your kwctl profile token (kwctl auth set-token).

Examples:
  kwctl secrets set-api-token --stdin
  kwctl secrets set-api-token --from-env KUBEWISE_API_TOKEN
  kwctl secrets set-api-token my-token`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		if secretsAPITokenFromStdin && len(args) == 1 {
			return fmt.Errorf("got TOKEN argument while --stdin is set; use either --stdin or pass TOKEN as an argument, not both")
		}

		tok, err := readAPITokenInput(cmd, args)
		if err != nil {
			return err
		}
		tok = strings.TrimSpace(tok)
		if tok == "" {
			return fmt.Errorf("token must not be empty")
		}

		secName, err := resolveAgentSecretName(cmd.Context(), "KUBEWISE_API_TOKEN")
		if err != nil {
			return err
		}
		if secName == "" {
			secName = "kubewise-secret"
		}

		if err := upsertSecretStringData(cmd.Context(), agentNS, secName, map[string]string{
			"api_token": tok,
		}); err != nil {
			return err
		}

		printOK(cmd.OutOrStdout(), "API token saved in secret %s/%s", agentNS, secName)
		if secretsAPITokenRestartAgent {
			depName, _ := resolveAgentDeploymentName(cmd.Context())
			if depName == "" {
				depName = "kubewise-agent"
			}
			if err := rolloutRestartDeployment(agentNS, depName); err != nil {
				return err
			}
			printOK(cmd.OutOrStdout(), "Agent restarted")
		} else {
			printHint(cmd.OutOrStdout(), "restart agent to apply: kubectl -n kubewise rollout restart deploy/<agent-deployment>")
		}
		return nil
	},
}

func readSecretInput(cmd *cobra.Command, args []string) (string, error) {
	switch {
	case secretsFromStdin:
		// Read everything (supports piped input with or without trailing newline).
		b, err := io.ReadAll(cmd.InOrStdin())
		if err != nil {
			return "", fmt.Errorf("read from stdin: %w", err)
		}
		return strings.TrimSpace(string(b)), nil
	case secretsFromEnv != "":
		return strings.TrimSpace(os.Getenv(secretsFromEnv)), nil
	case len(args) == 1:
		return strings.TrimSpace(args[0]), nil
	default:
		// Interactive prompt (TTY only). This is the most developer-friendly path.
		in := cmd.InOrStdin()
		if f, ok := in.(*os.File); ok && term.IsTerminal(int(f.Fd())) {
			_, _ = fmt.Fprint(cmd.ErrOrStderr(), "Paste OpenRouter API key (input hidden): ")
			b, err := term.ReadPassword(int(f.Fd()))
			_, _ = fmt.Fprintln(cmd.ErrOrStderr(), "")
			if err != nil {
				return "", fmt.Errorf("read key: %w", err)
			}
			return strings.TrimSpace(string(b)), nil
		}
		return "", fmt.Errorf("provide KEY, or use --stdin / --from-env")
	}
}

func readAPITokenInput(cmd *cobra.Command, args []string) (string, error) {
	switch {
	case secretsAPITokenFromStdin:
		b, err := io.ReadAll(cmd.InOrStdin())
		if err != nil {
			return "", fmt.Errorf("read from stdin: %w", err)
		}
		return strings.TrimSpace(string(b)), nil
	case secretsAPITokenFromEnv != "":
		return strings.TrimSpace(os.Getenv(secretsAPITokenFromEnv)), nil
	case len(args) == 1:
		return strings.TrimSpace(args[0]), nil
	default:
		in := cmd.InOrStdin()
		if f, ok := in.(*os.File); ok && term.IsTerminal(int(f.Fd())) {
			_, _ = fmt.Fprint(cmd.ErrOrStderr(), "Paste agent API token (input hidden): ")
			b, err := term.ReadPassword(int(f.Fd()))
			_, _ = fmt.Fprintln(cmd.ErrOrStderr(), "")
			if err != nil {
				return "", fmt.Errorf("read token: %w", err)
			}
			return strings.TrimSpace(string(b)), nil
		}
		return "", fmt.Errorf("provide TOKEN, or use --stdin / --from-env")
	}
}

var openRouterKeyRe = regexp.MustCompile(`^sk-or-v1-[A-Za-z0-9_-]{10,}$`)

func validateOpenRouterKey(key string) error {
	// Don’t over-validate, but catch obvious copy/paste mistakes.
	if strings.ContainsAny(key, " \t\r\n") {
		return fmt.Errorf("key looks malformed (contains whitespace)")
	}
	if !openRouterKeyRe.MatchString(key) {
		return fmt.Errorf("key doesn't look like an OpenRouter key (expected prefix sk-or-v1-...)")
	}
	return nil
}

// resolveAgentSecretName inspects the running deployment and returns the secret name referenced by env var envName.
func resolveAgentSecretName(ctx context.Context, envName string) (string, error) {
	type dep struct {
		Spec struct {
			Template struct {
				Spec struct {
					Containers []struct {
						Name string `json:"name"`
						Env  []struct {
							Name      string `json:"name"`
							ValueFrom *struct {
								SecretKeyRef *struct {
									Name string `json:"name"`
									Key  string `json:"key"`
								} `json:"secretKeyRef"`
							} `json:"valueFrom"`
						} `json:"env"`
					} `json:"containers"`
				} `json:"spec"`
			} `json:"template"`
		} `json:"spec"`
	}

	depName, err := resolveAgentDeploymentName(ctx)
	if err != nil {
		return "", err
	}
	b, err := kubectlJSON(ctx, "get", "deployment", depName, "-n", agentNS, "-o", "json")
	if err != nil {
		return "", err
	}
	var d dep
	if err := json.Unmarshal(b, &d); err != nil {
		return "", fmt.Errorf("parse deployment json: %w", err)
	}
	for _, c := range d.Spec.Template.Spec.Containers {
		if c.Name != "agent" {
			continue
		}
		for _, e := range c.Env {
			if e.Name != envName || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil {
				continue
			}
			return strings.TrimSpace(e.ValueFrom.SecretKeyRef.Name), nil
		}
	}
	return "", nil
}

func resolveAgentDeploymentName(ctx context.Context) (string, error) {
	// Prefer whatever the user is targeting for service (Helm default is "kubewise",
	// manifests default is "kubewise-agent"). Deployment name may differ from service,
	// but these two cover the common cases.
	candidates := []string{"kubewise-agent", "kubewise", agentSvc}
	seen := map[string]bool{}
	for _, name := range candidates {
		name = strings.TrimSpace(name)
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		if _, err := kubectlJSON(ctx, "get", "deployment", name, "-n", agentNS, "-o", "json"); err == nil {
			return name, nil
		}
	}
	return "", fmt.Errorf("could not find agent deployment in namespace %q (tried: kubewise-agent, kubewise)", agentNS)
}

func upsertSecretStringData(ctx context.Context, namespace, name string, stringData map[string]string) error {
	// Fetch existing secret (if any) to preserve other keys.
	type secret struct {
		APIVersion string `json:"apiVersion"`
		Kind       string `json:"kind"`
		Metadata   struct {
			Name      string `json:"name"`
			Namespace string `json:"namespace"`
		} `json:"metadata"`
		Type       string            `json:"type"`
		StringData map[string]string `json:"stringData,omitempty"`
	}

	s := secret{
		APIVersion: "v1",
		Kind:       "Secret",
		Type:       "Opaque",
		StringData: map[string]string{},
	}
	s.Metadata.Name = name
	s.Metadata.Namespace = namespace

	for k, v := range stringData {
		s.StringData[k] = v
	}

	body, err := json.Marshal(s)
	if err != nil {
		return err
	}
	return kubectlApplyJSON(ctx, body)
}

func rolloutRestartDeployment(namespace, name string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if _, err := kubectlJSON(ctx, "rollout", "restart", "deployment/"+name, "-n", namespace); err != nil {
		return err
	}
	_, err := kubectlJSON(ctx, "rollout", "status", "deployment/"+name, "-n", namespace, "--timeout=120s")
	return err
}

func kubectlJSON(ctx context.Context, args ...string) ([]byte, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	c := exec.CommandContext(ctx, "kubectl", args...)
	var out bytes.Buffer
	var stderr bytes.Buffer
	c.Stdout = &out
	c.Stderr = &stderr
	if err := c.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return nil, fmt.Errorf("kubectl %s: %s", strings.Join(args, " "), msg)
	}
	return out.Bytes(), nil
}

func kubectlApplyJSON(ctx context.Context, obj []byte) error {
	if ctx == nil {
		ctx = context.Background()
	}
	c := exec.CommandContext(ctx, "kubectl", "apply", "-f", "-")
	c.Stdin = bytes.NewReader(obj)
	var stderr bytes.Buffer
	c.Stdout = ioDiscard{}
	c.Stderr = &stderr
	if err := c.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return fmt.Errorf("kubectl apply: %s", msg)
	}
	return nil
}

type ioDiscard struct{}

func (ioDiscard) Write(p []byte) (int, error) { return len(p), nil }
