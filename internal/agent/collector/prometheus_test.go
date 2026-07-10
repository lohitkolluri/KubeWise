package collector

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
)

func fakePrometheusServer(t *testing.T, responseJSON string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/query" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(responseJSON))
			return
		}
		http.NotFound(w, r)
	}))
}

func TestNewPrometheusCollector(t *testing.T) {
	f, err := os.CreateTemp("", "kw-store-*.db")
	if err != nil {
		t.Fatal(err)
	}
	_ = f.Close()
	defer os.Remove(f.Name())

	s, err := store.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	collector, err := NewPrometheusCollector("http://localhost:9090", s, nil)
	if err != nil {
		t.Fatalf("NewPrometheusCollector: %v", err)
	}
	if collector == nil {
		t.Fatal("expected non-nil collector")
	}
}

func TestCollectMetrics_ValidResponse(t *testing.T) {
	resp := `{
		"status": "success",
		"data": {
			"resultType": "vector",
			"result": [
				{
					"metric": {"pod": "nginx-abc", "namespace": "default"},
					"value": [1712345678.123, "0.85"]
				}
			]
		}
	}`
	server := fakePrometheusServer(t, resp)
	defer server.Close()

	f, err := os.CreateTemp("", "kw-store-*.db")
	if err != nil {
		t.Fatal(err)
	}
	_ = f.Close()
	defer os.Remove(f.Name())

	s, err := store.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	collector, err := NewPrometheusCollector(server.URL, s, nil)
	if err != nil {
		t.Fatal(err)
	}

	results, err := collector.CollectMetrics(context.Background())
	if err != nil {
		t.Fatalf("CollectMetrics: %v", err)
	}

	// At least one query should succeed
	found := false
	for _, r := range results {
		if len(r.Values) > 0 {
			found = true
			break
		}
	}
	if !found {
		t.Fatal("expected at least one result with values, all were empty")
	}
}

func TestCollectMetrics_ServerError(t *testing.T) {
	server := fakePrometheusServer(t, `{"status":"error","errorType":"internal","error":"something broke"}`)
	defer server.Close()

	f, err := os.CreateTemp("", "kw-store-*.db")
	if err != nil {
		t.Fatal(err)
	}
	_ = f.Close()
	defer os.Remove(f.Name())

	s, err := store.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	collector, err := NewPrometheusCollector(server.URL, s, nil)
	if err != nil {
		t.Fatal(err)
	}

	// Total failure should return an error
	results, err := collector.CollectMetrics(context.Background())
	if err == nil {
		t.Fatal("CollectMetrics should return error when all queries fail")
	}
	if len(results) != 0 {
		t.Fatalf("expected no results on total failure, got %d", len(results))
	}
}

func TestCollectQuery(t *testing.T) {
	resp := `{
		"status": "success",
		"data": {
			"resultType": "vector",
			"result": [
				{
					"metric": {"pod": "my-pod"},
					"value": [1712345678.123, "0.95"]
				}
			]
		}
	}`
	server := fakePrometheusServer(t, resp)
	defer server.Close()

	f, err := os.CreateTemp("", "kw-store-*.db")
	if err != nil {
		t.Fatal(err)
	}
	_ = f.Close()
	defer os.Remove(f.Name())

	s, err := store.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	collector, err := NewPrometheusCollector(server.URL, s, nil)
	if err != nil {
		t.Fatal(err)
	}

	result, err := collector.CollectQuery(context.Background(), "test_query", "up")
	if err != nil {
		t.Fatalf("CollectQuery: %v", err)
	}
	if result.Name != "test_query" {
		t.Fatalf("expected name test_query, got %s", result.Name)
	}
	if len(result.Values) != 1 {
		t.Fatalf("expected 1 value, got %d", len(result.Values))
	}
	if result.Values[0].Value != 0.95 {
		t.Fatalf("expected value 0.95, got %f", result.Values[0].Value)
	}
}

func TestPrometheusCollector_FilterWatchNamespaces(t *testing.T) {
	col := &PrometheusCollector{watchNamespaces: []string{"demo"}}
	filtered := col.filterResult(MetricResult{
		Name: "pod_cpu",
		Values: []MetricPoint{
			{Labels: map[string]string{"namespace": "demo", "pod": "a"}},
			{Labels: map[string]string{"namespace": "prod", "pod": "b"}},
			{Labels: map[string]string{"node": "n1"}},
		},
	})
	if len(filtered.Values) != 2 {
		t.Fatalf("expected 2 values (demo + node-level), got %d", len(filtered.Values))
	}
}
