package cli

import (
	"fmt"
	"os/exec"

	"github.com/spf13/cobra"
)

var doctorCmd = &cobra.Command{
	Use:   "doctor",
	Short: "Check toolchain and agent connectivity",
	Long: `Verify prerequisites for kwctl install and local development.

Examples:
  kwctl doctor
  kwctl doctor --agent-url http://127.0.0.1:18080`,
	RunE: runDoctor,
}

func init() {
	rootCmd.AddCommand(doctorCmd)
}

func runDoctor(cmd *cobra.Command, _ []string) error {
	out := cmd.OutOrStdout()
	ok := true

	check := func(name string, err error) {
		if err != nil {
			_, _ = fmt.Fprintf(out, "✗ %s: %v\n", name, err)
			ok = false
		} else {
			_, _ = fmt.Fprintf(out, "✓ %s\n", name)
		}
	}

	if _, err := exec.LookPath("kubectl"); err != nil {
		check("kubectl", err)
	} else {
		check("kubectl", nil)
		if kc, err := newKubeClient(); err != nil {
			check("kubernetes cluster", err)
		} else if err := kc.PingCluster(cmd.Context()); err != nil {
			check("kubernetes cluster", err)
		} else {
			check("kubernetes cluster", nil)
		}
	}

	for _, tool := range []string{"helm", "kind", "docker"} {
		if _, err := exec.LookPath(tool); err != nil {
			_, _ = fmt.Fprintf(out, "○ %s (optional for local dev)\n", tool)
		} else if tool == "docker" {
			if err := exec.Command("docker", "info").Run(); err != nil {
				check("docker daemon", fmt.Errorf("not running — start Docker Desktop"))
			} else {
				check("docker daemon", nil)
			}
		} else {
			check(tool, nil)
		}
	}

	if _, err := fetchHealth(); err != nil {
		_, _ = fmt.Fprintf(out, "✗ agent at %s: %v\n", resolveAgentURL(), err)
		_, _ = fmt.Fprintln(out, "  → run: kwctl up   (or start the agent / port-forward)")
		ok = false
	} else {
		_, _ = fmt.Fprintf(out, "✓ agent at %s\n", resolveAgentURL())
		if st, err := fetchStatus(); err == nil {
			_, _ = fmt.Fprintf(out, "  scrapes: %d  uptime: %s\n", st.Scrapes, st.Uptime)
		}
	}

	if !ok {
		return fmt.Errorf("some checks failed")
	}
	_, _ = fmt.Fprintln(out, "\nAll checks passed.")
	return nil
}
