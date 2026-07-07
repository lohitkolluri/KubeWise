package bootstrap

import (
	"os"
	"path/filepath"
	"testing"
)

func TestInit_DefaultConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("KUBEWISE_DATA_DIR", dir)
	t.Setenv("KUBEWISE_CONFIG_PATH", "")
	t.Setenv("KUBEWISE_REQUIRE_API_TOKEN", "")

	rt, err := Init()
	if err != nil {
		t.Fatal(err)
	}
	defer rt.Store.Close()

	if rt.Config.PrometheusAddress == "" {
		t.Fatal("expected default prometheus address")
	}
	if _, err := os.Stat(filepath.Join(dir, "agent.db")); err != nil {
		t.Fatalf("expected db file: %v", err)
	}
}

func TestInit_RequireAPITokenFailsClosed(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("KUBEWISE_DATA_DIR", dir)
	t.Setenv("KUBEWISE_REQUIRE_API_TOKEN", "true")
	t.Setenv("KUBEWISE_API_TOKEN", "")

	if _, err := Init(); err == nil {
		t.Fatal("expected error when require token set without token")
	}
}
