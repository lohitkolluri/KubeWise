package bootstrap

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
)

func TestMergeObservabilityFromFile(t *testing.T) {
	t.Parallel()

	dst := defaultConfig()
	file := defaultConfig()
	file.LokiURL = "http://loki:3100"
	file.TempoURL = "http://tempo:3200"

	if !mergeObservabilityFromFile(dst, file) {
		t.Fatal("expected merge to report changes")
	}
	if dst.LokiURL != file.LokiURL || dst.TempoURL != file.TempoURL {
		t.Fatalf("got loki=%q tempo=%q", dst.LokiURL, dst.TempoURL)
	}
	if mergeObservabilityFromFile(dst, file) {
		t.Fatal("expected no-op on second merge")
	}
}

func TestInit_MergesObservabilityFromMountedConfig(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(cfgPath, []byte(`scrape_interval: 30s
prometheus_address: http://prom:9090
loki_url: http://loki-gateway:80
tempo_url: http://tempo:3200
`), 0o600); err != nil {
		t.Fatal(err)
	}

	t.Setenv("KUBEWISE_DATA_DIR", dir)
	t.Setenv("KUBEWISE_CONFIG_PATH", cfgPath)
	t.Setenv("KUBEWISE_REQUIRE_API_TOKEN", "")

	rt1, err := Init()
	if err != nil {
		t.Fatal(err)
	}
	if rt1.Config.LokiURL != "http://loki-gateway:80" {
		t.Fatalf("seed loki_url = %q", rt1.Config.LokiURL)
	}
	_ = rt1.Store.Close()

	// Simulate store seeded without observability URLs (older deploy).
	s, err := store.Open(dir + "/agent.db")
	if err != nil {
		t.Fatal(err)
	}
	stale := defaultConfig()
	stale.ScrapeInterval = "30s"
	if err := s.SaveConfig(stale); err != nil {
		t.Fatal(err)
	}
	_ = s.Close()

	rt2, err := Init()
	if err != nil {
		t.Fatal(err)
	}
	defer rt2.Store.Close()

	if rt2.Config.LokiURL != "http://loki-gateway:80" {
		t.Fatalf("merged loki_url = %q, want http://loki-gateway:80", rt2.Config.LokiURL)
	}
	if rt2.Config.TempoURL != "http://tempo:3200" {
		t.Fatalf("merged tempo_url = %q", rt2.Config.TempoURL)
	}
}
