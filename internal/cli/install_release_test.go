package cli

import (
	"bytes"
	"os"
	"os/exec"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// nextPatchTag
// ---------------------------------------------------------------------------

// newGitRunner creates a git command runner in a temp directory with isolated
// config, so tests don't depend on the user's global git configuration.
func newGitRunner(t *testing.T, dir string) func(args ...string) {
	t.Helper()
	gitEnv := append(
		[]string{"GIT_CONFIG_NOSYSTEM=1", "HOME=" + dir},
		os.Environ()...,
	)
	return func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...) //nolint:gosec // args are controlled test data
		cmd.Dir = dir
		cmd.Env = gitEnv
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %s: %v", args, string(out), err)
		}
	}
}

func TestNextPatchTag(t *testing.T) {
	tests := []struct {
		latest string
		want   string
	}{
		{"v1.0.2", "v1.0.3"},
		{"v1.0.9", "v1.0.10"},
		{"v1.0.0", "v1.0.1"},
		{"v1.0.99", "v1.0.100"},
		{"", "v1.0.2"},       // no tags yet → chart version fallback
		{"v1.0.1", "v1.0.2"}, // matches chart version 1.0.1
	}
	for _, tc := range tests {
		got := nextPatchTag(tc.latest)
		if got != tc.want {
			t.Errorf("nextPatchTag(%q) = %q, want %q", tc.latest, got, tc.want)
		}
	}
}

// ---------------------------------------------------------------------------
// parseV10Patch
// ---------------------------------------------------------------------------

func TestParseV10Patch(t *testing.T) {
	tests := []struct {
		tag  string
		want int
	}{
		{"v1.0.0", 0},
		{"v1.0.2", 2},
		{"v1.0.10", 10},
		{"v1.0.999", 999},
		{"v1.1.0", -1},     // minor mismatch
		{"v2.0.0", -1},     // major mismatch
		{"v1.0.3-rc1", -1}, // pre-release suffix
		{"random", -1},
		{"", -1},
	}
	for _, tc := range tests {
		got := parseV10Patch(tc.tag)
		if got != tc.want {
			t.Errorf("parseV10Patch(%q) = %d, want %d", tc.tag, got, tc.want)
		}
	}
}

// ---------------------------------------------------------------------------
// latestReleaseTag — uses a temp git repo
// ---------------------------------------------------------------------------

func TestLatestReleaseTag(t *testing.T) {
	dir := t.TempDir()

	// Initialize a git repo in the temp dir and set up git config so
	// the test doesn't depend on the user's global git config.
	runGit := newGitRunner(t, dir)

	runGit("init")
	// Override internal git config for the test repo so we don't need
	// the user's credentials.
	runGit("config", "user.email", "test@test")
	runGit("config", "user.name", "Test")

	// Create an initial commit so tags have an anchor.
	runGit("commit", "--allow-empty", "-m", "initial")

	// Create a file so we have content.
	if err := os.WriteFile(dir+"/test.txt", []byte("hello"), 0o600); err != nil {
		t.Fatal(err)
	}
	runGit("add", "test.txt")
	runGit("commit", "-m", "add test.txt")

	// Create a few v1.0.x tags (out of order to test max selection).
	runGit("tag", "v1.0.1")
	runGit("tag", "v1.0.5")
	runGit("tag", "v1.0.3")

	origDir, _ := os.Getwd()
	_ = os.Chdir(dir)
	t.Cleanup(func() { _ = os.Chdir(origDir) })

	got := latestReleaseTag()
	if got != "v1.0.5" {
		t.Errorf("latestReleaseTag() = %q, want %q", got, "v1.0.5")
	}

	// Test with no matching tags: add a non-matching tag, ensure still returns v1.0.5.
	runGit("tag", "v2.0.0")
	got = latestReleaseTag()
	if got != "v1.0.5" {
		t.Errorf("latestReleaseTag() with v2.0.0 present = %q, want %q", got, "v1.0.5")
	}
}

func TestLatestReleaseTag_NoTags(t *testing.T) {
	dir := t.TempDir()

	runGit := newGitRunner(t, dir)

	runGit("init")
	runGit("config", "user.email", "test@test")
	runGit("config", "user.name", "Test")
	runGit("commit", "--allow-empty", "-m", "initial")

	origDir, _ := os.Getwd()
	_ = os.Chdir(dir)
	t.Cleanup(func() { _ = os.Chdir(origDir) })

	got := latestReleaseTag()
	if got != "" {
		t.Errorf("latestReleaseTag() with no tags = %q, want empty", got)
	}
}

// ---------------------------------------------------------------------------
// detectInstalled — non-cluster-dependent tests
// ---------------------------------------------------------------------------

func TestDetectInstalled_NoTools(t *testing.T) {
	// Temporarily remove helm and kubectl from PATH to verify the function
	// returns false without error when the tools are unavailable.
	origPath := os.Getenv("PATH")
	_ = os.Setenv("PATH", "/dev/null")
	t.Cleanup(func() { _ = os.Setenv("PATH", origPath) })

	installed, path := detectInstalled()
	if installed {
		t.Errorf("detectInstalled() with no tools = (true, %q), want (false, \"\")", path)
	}
	if path != "" {
		t.Errorf("detectInstalled() with no tools path = %q, want \"\"", path)
	}
}

func TestDetectInstalled_NoCluster(t *testing.T) {
	// This test requires helm or kubectl on PATH. If neither is available, skip.
	hasHelm := exec.Command("helm", "version").Run() == nil
	hasKubectl := exec.Command("kubectl", "version", "--client").Run() == nil

	if !hasHelm && !hasKubectl {
		t.Skip("neither helm nor kubectl on PATH — skipping detectInstalled test")
	}

	// Both tools are available but pointing at a cluster that likely doesn't
	// have KubeWise installed. The function should return false without error.
	installed, path := detectInstalled()
	if installed {
		t.Logf("detectInstalled returned (true, %q) — cluster may actually have KubeWise installed; this is not a test failure", path)
		// Not a failure — the test environment might legitimately have an install.
		return
	}
	if path != "" {
		t.Errorf("detectInstalled() path = %q, want \"\" when not installed", path)
	}
}

// ---------------------------------------------------------------------------
// gitRelease — validation helpers (not running real git mutations)
// ---------------------------------------------------------------------------

func TestGitRelease_RequiresGit(t *testing.T) {
	// Ensure gitRelease fails fast when git is not on PATH.
	origPath := os.Getenv("PATH")
	_ = os.Setenv("PATH", "/dev/null")
	t.Cleanup(func() { _ = os.Setenv("PATH", origPath) })

	var buf bytes.Buffer
	err := gitRelease("v1.0.99", true, &buf)
	if err == nil {
		t.Fatal("gitRelease should fail when git is not on PATH")
	}
	if !strings.Contains(err.Error(), "git not found") {
		t.Errorf("gitRelease error = %q, want 'git not found'", err.Error())
	}
}

// ---------------------------------------------------------------------------
// redeployWithTag — validates error handling
// ---------------------------------------------------------------------------

func TestRedeployWithTag_UnknownPath(t *testing.T) {
	var buf bytes.Buffer
	err := redeployWithTag("1.0.99", "nonexistent", &buf)
	if err == nil {
		t.Fatal("redeployWithTag should fail with unknown path")
	}
	if !strings.Contains(err.Error(), "unknown install path") {
		t.Errorf("redeployWithTag error = %q, want 'unknown install path'", err.Error())
	}
}
