package cli

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/spf13/cobra"
)

var upForeground bool
var upLocalPort int
var upRemotePort int
var upPersistProfile bool

func init() {
	upCmd.Flags().BoolVar(&upForeground, "foreground", false, "run port-forward in foreground (blocks until Ctrl+C)")
	upCmd.Flags().IntVar(&upLocalPort, "local-port", 8080, "local port for agent port-forward")
	upCmd.Flags().IntVar(&upRemotePort, "remote-port", 8080, "remote agent service port")
	upCmd.Flags().BoolVar(&upPersistProfile, "persist-profile", false, "persist agent-url=http://localhost:<local-port> into the active profile")
	rootCmd.AddCommand(upCmd, downCmd)
}

var upCmd = &cobra.Command{
	Use:   "up",
	Short: "Connect to the agent (port-forward if needed)",
	Long: `Start kubectl port-forward to the agent API when localhost:8080 is not reachable,
wait until the agent responds, then exit so you can run kwctl ui or other commands.

Examples:
  kwctl up          # background port-forward + health check
  kwctl up -f       # foreground port-forward (keep terminal open)
  kwctl ui          # after kwctl up`,
	RunE: runUp,
}

var downCmd = &cobra.Command{
	Use:   "down",
	Short: "Stop background port-forward started by kwctl up",
	RunE:  runDown,
}

func runUp(cmd *cobra.Command, _ []string) error {
	out := cmd.OutOrStdout()
	if err := requireKubectl(); err != nil {
		return err
	}
	if upLocalPort <= 0 || upLocalPort > 65535 {
		return fmt.Errorf("invalid --local-port %d", upLocalPort)
	}
	if upRemotePort <= 0 || upRemotePort > 65535 {
		return fmt.Errorf("invalid --remote-port %d", upRemotePort)
	}

	// If the user is port-forwarding to a non-default port and didn't explicitly
	// set --agent-url, point the client at the correct localhost port for this run.
	if agentURL == "" || agentURL == defaultAgentURL {
		if upLocalPort != 8080 {
			agentURL = fmt.Sprintf("http://localhost:%d", upLocalPort)
		}
	}

	if _, err := fetchHealth(); err == nil {
		_, _ = fmt.Fprintln(out, "✓ Agent already reachable at", resolveAgentURL())
		return printUpHints(out)
	}

	if upForeground {
		return runPortForwardForeground(out)
	}

	if err := stopBackgroundPortForward(); err != nil {
		return err
	}
	if err := startBackgroundPortForward(); err != nil {
		return fmt.Errorf("port-forward: %w", err)
	}
	_, _ = fmt.Fprintln(out, "… waiting for agent at", resolveAgentURL())
	if err := waitForAgent(30 * time.Second); err != nil {
		_ = stopBackgroundPortForward()
		return err
	}
	if !portForwardRunning() {
		_ = stopBackgroundPortForward()
		return fmt.Errorf("port-forward exited early — see %s", mustPortForwardLog())
	}
	_, _ = fmt.Fprintln(out, "✓ Port-forward running (kwctl down to stop)")

	if upPersistProfile {
		if err := setProfileField(profileName, "agent-url", fmt.Sprintf("http://localhost:%d", upLocalPort)); err != nil {
			return fmt.Errorf("persist profile agent-url: %w", err)
		}
	}
	return printUpHints(out)
}

func runDown(_ *cobra.Command, _ []string) error {
	if err := stopBackgroundPortForward(); err != nil {
		return err
	}
	fmt.Println("Port-forward stopped.")
	return nil
}

func printUpHints(out io.Writer) error {
	_, _ = fmt.Fprintln(out, "")
	_, _ = fmt.Fprintln(out, "Next:")
	_, _ = fmt.Fprintln(out, "  kwctl connect    # verify")
	_, _ = fmt.Fprintln(out, "  kwctl ui         # control center")
	_, _ = fmt.Fprintln(out, "  kwctl status     # quick summary")
	return nil
}

func waitForAgent(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	var lastErr error
	for time.Now().Before(deadline) {
		if _, err := fetchHealth(); err == nil {
			return nil
		} else {
			lastErr = err
		}
		time.Sleep(500 * time.Millisecond)
	}
	if lastErr != nil {
		return fmt.Errorf("agent not reachable: %w", lastErr)
	}
	return fmt.Errorf("agent not reachable within %s", timeout)
}

func portForwardPIDPath() (string, error) {
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".config")
	}
	dir := filepath.Join(base, "kwctl")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	// Include port in filename to avoid collisions if user changes --local-port.
	return filepath.Join(dir, fmt.Sprintf("port-forward-%d.pid", upLocalPort)), nil
}

func startBackgroundPortForward() error {
	args := portForwardArgs()
	logPath, err := portForwardLogPath()
	if err != nil {
		return err
	}
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	cmd := exec.Command("kubectl", args...)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		_ = logFile.Close()
		return err
	}
	_ = logFile.Close()
	pidPath, err := portForwardPIDPath()
	if err != nil {
		_ = cmd.Process.Kill()
		return err
	}
	return os.WriteFile(pidPath, []byte(strconv.Itoa(cmd.Process.Pid)), 0o600)
}

func portForwardLogPath() (string, error) {
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".config")
	}
	dir := filepath.Join(base, "kwctl")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return filepath.Join(dir, fmt.Sprintf("port-forward-%d.log", upLocalPort)), nil
}

func portForwardRunning() bool {
	pidPath, err := portForwardPIDPath()
	if err != nil {
		return false
	}
	data, err := os.ReadFile(pidPath)
	if err != nil {
		return false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || pid <= 0 {
		return false
	}
	if syscall.Kill(pid, 0) != nil {
		return false
	}
	// Best-effort guard against PID reuse: verify the command looks like a kubectl port-forward.
	// If ps is unavailable or parsing fails, fall back to the existence check above.
	out, err := exec.Command("ps", "-p", strconv.Itoa(pid), "-o", "command=").Output()
	if err != nil {
		return true
	}
	cmdline := string(out)
	if !strings.Contains(cmdline, "kubectl") {
		return false
	}
	if !strings.Contains(cmdline, "port-forward") {
		return false
	}
	return true
}

func mustPortForwardLog() string {
	p, err := portForwardLogPath()
	if err != nil {
		return "~/.config/kwctl/port-forward.log"
	}
	return p
}

func runPortForwardForeground(out io.Writer) error {
	_, _ = fmt.Fprintf(out, "Forwarding %s/svc/%s → localhost:%d (Ctrl+C to stop)\n", agentNS, agentSvc, upLocalPort)
	args := portForwardArgs()
	cmd := exec.Command("kubectl", args...)
	cmd.Stdout = out
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func portForwardArgs() []string {
	args := []string{}
	if kubeconfig != "" {
		args = append(args, "--kubeconfig", kubeconfig)
	}
	if contextName != "" {
		args = append(args, "--context", contextName)
	}
	args = append(args, "-n", agentNS, "port-forward", "svc/"+agentSvc, fmt.Sprintf("%d:%d", upLocalPort, upRemotePort))
	return args
}

func stopBackgroundPortForward() error {
	pidPath, err := portForwardPIDPath()
	if err != nil {
		return err
	}
	data, err := os.ReadFile(pidPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		_ = os.Remove(pidPath)
		return nil
	}
	// Negative PID kills the process group started with Setpgid.
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	_ = os.Remove(pidPath)
	return nil
}
