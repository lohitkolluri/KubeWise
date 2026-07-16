package cli

import (
	"errors"
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
	printBanner(out)
	printSection(out, "Environment checks")
	ok := true

	check := func(name string, err error) {
		if err != nil {
			printFail(out, "%s: %v", name, err)
			ok = false
			return
		}
		printOK(out, "%s", name)
	}

	_, kubectlErr := exec.LookPath("kubectl")
	check("kubectl", kubectlErr)
	if kubectlErr == nil {
		kc, err := newKubeClient()
		if err != nil {
			check("kubernetes cluster", err)
		} else {
			check("kubernetes cluster", kc.PingCluster(cmd.Context()))
		}
	}

	for _, tool := range []string{"helm", "kind", "docker"} {
		_, err := exec.LookPath(tool)
		if err != nil {
			printOptional(out, "%s (optional for local dev)", tool)
			continue
		}
		if tool == "docker" {
			check("docker daemon", exec.Command("docker", "info").Run())
		} else {
			check(tool, nil)
		}
	}

	printSection(out, "Agent")
	if _, err := fetchHealth(); err != nil {
		printFail(out, "agent at %s: %v", resolveAgentURL(), err)
		printHint(out, "run: kwctl up   (or start the agent / port-forward)")
		ok = false
	} else {
		printOK(out, "agent at %s", resolveAgentURL())
		if st, err := fetchStatus(); err == nil {
			printKV(out, "Scrapes:", fmt.Sprintf("%d", st.Scrapes))
			printKV(out, "Uptime:", st.Uptime)
		}
	}

	if !ok {
		return errors.New("some checks failed")
	}
	printOK(out, "All checks passed")
	printNextSteps(out, "kwctl install", "kwctl ui")
	return nil
}
