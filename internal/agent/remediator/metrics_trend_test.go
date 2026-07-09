package remediator

import (
	"testing"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
)

func TestMetricTrendDirection(t *testing.T) {
	pts := []store.MetricPoint{
		{Value: 100},
		{Value: 102},
		{Value: 101},
		{Value: 120},
	}
	if got := metricTrendDirection(pts); got != "rising" {
		t.Fatalf("trend = %q, want rising", got)
	}

	falling := []store.MetricPoint{
		{Value: 100},
		{Value: 99},
		{Value: 98},
		{Value: 80},
	}
	if got := metricTrendDirection(falling); got != "falling" {
		t.Fatalf("trend = %q, want falling", got)
	}

	outlierStable := []store.MetricPoint{
		{Value: 200}, // outlier at start
		{Value: 100},
		{Value: 101},
		{Value: 102},
	}
	if got := metricTrendDirection(outlierStable); got != "stable" {
		t.Fatalf("trend = %q, want stable (median baseline)", got)
	}
}

func TestIsBuiltInProtectedNamespace(t *testing.T) {
	for _, ns := range []string{"kube-system", "istio-system", "cert-manager"} {
		if !isBuiltInProtectedNamespace(ns) {
			t.Fatalf("expected %q to be protected", ns)
		}
	}
	if isBuiltInProtectedNamespace("default") {
		t.Fatal("default should not be protected")
	}
}
