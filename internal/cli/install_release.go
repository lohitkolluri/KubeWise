package cli

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

// ---------------------------------------------------------------------------
// Existing-install detection
// ---------------------------------------------------------------------------

// detectInstalled checks whether a KubeWise installation already exists in the
// target cluster. It returns (true, "helm") if the Helm release "kubewise" is
// found, (true, "kustomize") if the kubewise-agent Deployment exists, and
// (false, "") otherwise. Detection shells out to helm/kubectl CLI and treats
// command-not-found or a non-zero exit as "not installed" — it is intentionally
// non-fatal and fast.
func detectInstalled() (bool, string) {
	// Helm path
	if _, err := exec.LookPath("helm"); err == nil {
		cmd := exec.Command("helm", "list", "-n", agentNS, "--deployed", "--failed", "--pending", "-q") //nolint:gosec // CLI detection
		out, err := cmd.Output()
		if err == nil {
			for _, line := range strings.Split(string(out), "\n") {
				if strings.TrimSpace(line) == "kubewise" {
					return true, "helm"
				}
			}
		}
	}

	// Kustomize/manifest path
	if _, err := exec.LookPath("kubectl"); err == nil {
		cmd := exec.Command("kubectl", "get", "deployment", "-n", agentNS, "kubewise-agent", "--no-headers") //nolint:gosec // CLI detection
		if err := cmd.Run(); err == nil {
			return true, "kustomize"
		}
	}

	return false, ""
}

// ---------------------------------------------------------------------------
// Git tag helpers
// ---------------------------------------------------------------------------

// latestReleaseTag runs git tag --list 'v1.0.*' and returns the maximum tag
// matching ^v1\.0\.\d+$ by patch version, or "" if none are found.
func latestReleaseTag() string {
	cmd := exec.Command("git", "tag", "--list", "v1.0.*") //nolint:gosec // release tagging
	out, err := cmd.Output()
	if err != nil {
		return ""
	}

	var best string
	var bestPatch int
	for _, line := range strings.Split(string(out), "\n") {
		tag := strings.TrimSpace(line)
		if tag == "" {
			continue
		}
		patch := parseV10Patch(tag)
		if patch < 0 {
			continue
		}
		if best == "" || patch > bestPatch {
			best = tag
			bestPatch = patch
		}
	}
	return best
}

// parseV10Patch extracts the patch number from a "v1.0.X" tag, or -1 if the
// tag doesn't match the format.
func parseV10Patch(tag string) int {
	if !strings.HasPrefix(tag, "v1.0.") {
		return -1
	}
	patchStr := strings.TrimPrefix(tag, "v1.0.")
	if patchStr == "" {
		return -1
	}
	// Ensure no prefix/suffix after the number (e.g. "v1.0.3-rc1" is rejected)
	if n, err := strconv.Atoi(patchStr); err == nil {
		return n
	}
	return -1
}

// nextPatchTag returns the next patch tag after latest.
//   v1.0.2 → v1.0.3
//   v1.0.9 → v1.0.10
//   ""     → v1.0.2 (chart fallback: current Chart.yaml version is 1.0.1)
func nextPatchTag(latest string) string {
	if latest == "" {
		return "v1.0.2"
	}
	patch := parseV10Patch(latest)
	if patch < 0 {
		return "v1.0.2"
	}
	return fmt.Sprintf("v1.0.%d", patch+1)
}

// ---------------------------------------------------------------------------
// Git release workflow (commit → push → tag → push-tag)
// ---------------------------------------------------------------------------

// gitRelease commits dirty changes, pushes them, creates a tag, and pushes
// the tag. It is the source-of-truth git release routine for kwctl install.
//
//   - If the working tree is dirty and yes is false, the user is prompted
//     interactively on stderr before any git mutation.
//   - If the working tree is clean, the commit step is skipped.
//   - Tags are never force-moved: if the tag already exists, the function
//     returns an error.
func gitRelease(tag string, yes bool, out io.Writer) error {
	// --- Check git availability ---
	if _, err := exec.LookPath("git"); err != nil {
		return fmt.Errorf("git not found in PATH")
	}

	// --- Determine current branch ---
	branchBytes, err := exec.Command("git", "rev-parse", "--abbrev-ref", "HEAD").Output() //nolint:gosec // git operations
	if err != nil {
		return fmt.Errorf("failed to determine current git branch: %w", err)
	}
	branch := strings.TrimSpace(string(branchBytes))

	// --- Check for dirty tree ---
	statusBytes, err := exec.Command("git", "status", "--porcelain").Output() //nolint:gosec // git operations
	if err != nil {
		return fmt.Errorf("failed to check git status: %w", err)
	}
	dirty := strings.TrimSpace(string(statusBytes)) != ""

	if dirty {
		// Print preview
		printSection(out, "Release preview")
		_, _ = fmt.Fprintf(out, "  Target tag:  %s\n", tag)
		_, _ = fmt.Fprintf(out, "  Branch:      %s\n", branch)
		_, _ = fmt.Fprintln(out, "")
		printOK(out, "Modified files (will be committed):")
		shortOut, err := exec.Command("git", "status", "--short").Output() //nolint:gosec // git operations
		if err == nil {
			for _, line := range strings.Split(string(shortOut), "\n") {
				if trimmed := strings.TrimSpace(line); trimmed != "" {
					_, _ = fmt.Fprintf(out, "    %s\n", trimmed)
				}
			}
		}

		if !yes {
			if !isTerminal(os.Stderr) {
				return fmt.Errorf("working tree is dirty and --yes is not set — aborting (use --yes to skip the confirmation)")
			}
			_, _ = fmt.Fprint(os.Stderr, mutedStyle.Render("Proceed with the release? [y/N] "))
			var response string
			_, _ = fmt.Scanln(&response)
			response = strings.TrimSpace(strings.ToLower(response))
			if response != "y" && response != "yes" {
				_, _ = fmt.Fprintln(out, "Release cancelled.")
				return nil
			}
		}

		// --- git add -A ---
		printOptional(out, "Staging all changes...")
		if err := exec.Command("git", "add", "-A").Run(); err != nil { //nolint:gosec // git operations
			return fmt.Errorf("git add -A failed: %w", err)
		}

		// --- git commit ---
		commitMsg := fmt.Sprintf("chore: release %s", tag)
		printOptional(out, "Committing: %s", commitMsg)
		commitCmd := exec.Command("git", "commit", "-m", commitMsg) //nolint:gosec // git operations
		commitOut, err := commitCmd.CombinedOutput()
		if err != nil {
			return fmt.Errorf("git commit failed: %s: %w", strings.TrimSpace(string(commitOut)), err)
		}
	} else {
		printOptional(out, "Working tree is clean — skipping commit")
	}

	// --- git push origin <branch> ---
	printOptional(out, "Pushing branch %q to origin...", branch)
	pushBranch := exec.Command("git", "push", "origin", branch) //nolint:gosec // git operations
	pushOut, err := pushBranch.CombinedOutput()
	if err != nil {
		stderr := strings.TrimSpace(string(pushOut))
		if strings.Contains(stderr, "has no upstream") || strings.Contains(stderr, "upstream") {
			return fmt.Errorf("branch %q has no upstream — push it first (git push --set-upstream origin %s)", branch, branch)
		}
		return fmt.Errorf("git push origin %s failed: %s: %w", branch, stderr, err)
	}
	printOK(out, "Branch %q pushed", branch)

	// --- Check tag does not already exist ---
	tagCheck := exec.Command("git", "tag", "--list", tag) //nolint:gosec // git operations
	existing, err := tagCheck.Output()
	if err == nil && strings.TrimSpace(string(existing)) == tag {
		return fmt.Errorf("tag %s already exists — delete locally (git tag -d %s) and remotely (git push origin :refs/tags/%s) if you intend to re-release", tag, tag, tag)
	}

	// --- git tag ---
	printOptional(out, "Creating tag %s...", tag)
	if err := exec.Command("git", "tag", tag).Run(); err != nil { //nolint:gosec // git operations
		return fmt.Errorf("git tag %s failed: %w", tag, err)
	}
	printOK(out, "Tag %s created", tag)

	// --- git push origin <tag> ---
	printOptional(out, "Pushing tag %s to origin...", tag)
	pushTag := exec.Command("git", "push", "origin", tag) //nolint:gosec // git operations
	tagOut, err := pushTag.CombinedOutput()
	if err != nil {
		return fmt.Errorf("git push origin %s failed: %s: %w", tag, strings.TrimSpace(string(tagOut)), err)
	}
	printOK(out, "Tag %s pushed", tag)

	return nil
}

// ---------------------------------------------------------------------------
// Redeploy with image tag
// ---------------------------------------------------------------------------

// redeployWithTag updates the existing KubeWise installation to use the given
// image tag. path is the install path detected by detectInstalled ("helm" or
// "kustomize").
//
// Helm path: reuses helmInstallWithValues to trigger a Helm upgrade with the
// new image tag. The --wait timeout doubles as the poll for CI to publish the
// image.
//
// Kustomize path: uses kubectl set image + rollout status.
func redeployWithTag(tag, path string, out io.Writer) error {
	switch path {
	case "helm":
		return redeployHelm(tag, out)
	case "kustomize":
		return redeployKustomize(tag, out)
	default:
		return fmt.Errorf("unknown install path %q", path)
	}
}

func redeployHelm(tag string, out io.Writer) error {
	chart := findHelmChartDir()
	if chart == "" {
		chart = fmt.Sprintf("https://github.com/lohitkolluri/KubeWise/charts/kubewise?ref=%s", installRef)
	}

	values := map[string]any{
		"image": map[string]any{
			"agent": map[string]any{
				"repository": "ghcr.io/lohitkolluri/kubewise-agent",
				"tag":        tag,
			},
			"forecaster": map[string]any{
				"tag": tag,
			},
		},
	}

	// Run dep update for local charts so subchart dependencies resolve
	if local := findHelmChartDir(); local != "" {
		_ = runHelmDepUpdate(local)
	}

	if err := helmInstallWithValues(out, chart, values); err != nil {
		return fmt.Errorf("helm redeploy failed: %w\n\nCI may still be building the image; re-run `kwctl install` once ghcr.io/lohitkolluri/kubewise-agent:%s is published", err, tag)
	}
	return nil
}

func redeployKustomize(tag string, out io.Writer) error {
	setCmds := []*exec.Cmd{
		exec.Command("kubectl", "set", "image", "deployment/kubewise-agent", //nolint:gosec // CLI tool
			"-n", agentNS, "agent=ghcr.io/lohitkolluri/kubewise-agent:"+tag,
			"forecaster=ghcr.io/lohitkolluri/kubewise-forecaster:"+tag),
	}

	for _, c := range setCmds {
		c.Stdout = io.Discard
		c.Stderr = io.Discard
		if err := c.Run(); err != nil {
			return fmt.Errorf("kubectl set image failed (CI may still be queued): %w\n\nRe-run `kwctl install` once the image ghcr.io/lohitkolluri/kubewise-agent:%s is published", err, tag)
		}
	}
	printOK(out, "Deployment image updated to :%s", tag)

	// Wait for rollout
	if err := runWithSpinner(out, fmt.Sprintf("Waiting for rollout to complete (timeout: %s)...", installWaitTimeout), func() error {
		cmd := exec.Command("kubectl", "rollout", "status", "deployment/kubewise-agent", //nolint:gosec // CLI tool
			"-n", agentNS, "--timeout", installWaitTimeout.String())
		cmd.Stdout = io.Discard

		// Capture stderr for diagnostics
		var stderrBuf bytes.Buffer
		cmd.Stderr = &stderrBuf
		if err := cmd.Run(); err != nil {
			if stderrBuf.Len() > 0 {
				_, _ = fmt.Fprintln(out, mutedStyle.Render(strings.TrimSpace(stderrBuf.String())))
			}
			return fmt.Errorf("rollout status failed: %w", err)
		}
		return nil
	}); err != nil {
		return fmt.Errorf("kustomize redeploy timeout/error: %w\n\nCI may still be building the image; re-run `kwctl install` once the ghcr.io/lohitkolluri/kubewise-agent:%s image is published", err, tag)
	}
	return nil
}


