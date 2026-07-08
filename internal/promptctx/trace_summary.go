package promptctx

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// TraceSummary summarizes a trace from Tempo relevant to an anomaly.
type TraceSummary struct {
	TraceID    string `json:"trace_id"`
	RootName   string `json:"root_name,omitempty"`
	DurationMs int64  `json:"duration_ms"`
	SpanCount  int    `json:"span_count"`
	StartTime  string `json:"start_time"` // RFC3339 string
	Services   int    `json:"services"`   // number of unique services in the trace
}

// fetchTraceContext queries Tempo for recent traces matching the namespace
// and pod. Returns nil gracefully when tempoURL is empty.
// Queries the last 15 minutes, max 10 traces.
func fetchTraceContext(ctx context.Context, tempoURL, namespace, pod string, since time.Duration) ([]TraceSummary, error) {
	if tempoURL == "" {
		return nil, nil
	}
	if namespace == "" && pod == "" {
		return nil, nil
	}

	start := time.Now().Add(-since)
	end := time.Now()

	// Tempo search API: /api/search?tags={logfmt tags}&start={unixSec}&end={unixSec}&limit=10
	u, err := url.Parse(tempoURL + "/api/search")
	if err != nil {
		return nil, fmt.Errorf("parse tempo URL: %w", err)
	}
	q := u.Query()
	q.Set("tags", buildTempoTags(namespace, pod))
	q.Set("start", fmt.Sprintf("%d", start.Unix()))
	q.Set("end", fmt.Sprintf("%d", end.Unix()))
	q.Set("limit", "10")
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("create tempo request: %w", err)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("query tempo: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return nil, fmt.Errorf("tempo returned %d: %s", resp.StatusCode, string(body))
	}

	var result tempoSearchResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode tempo response: %w", err)
	}

	return flattenTraceSummaries(result), nil
}

// buildTempoTags builds logfmt-style tags for Tempo search.
// Tempo search uses tags like: service.name=foo namespace=bar
func buildTempoTags(namespace, pod string) string {
	tags := ""
	if namespace != "" {
		tags += "namespace=" + namespace
	}
	if pod != "" {
		if tags != "" {
			tags += " "
		}
		tags += "pod=" + pod
	}
	return tags
}

// flattenTraceSummaries extracts trace summaries from the Tempo search response.
func flattenTraceSummaries(resp tempoSearchResponse) []TraceSummary {
	if len(resp.Traces) == 0 {
		return nil
	}

	summaries := make([]TraceSummary, 0, len(resp.Traces))
	for _, t := range resp.Traces {
		ts := TraceSummary{
			TraceID:    t.TraceID,
			RootName:   t.RootServiceName,
			DurationMs: t.DurationMs,
			SpanCount:  t.SpanCount,
			StartTime:  formatUnixMs(t.StartTime),
			Services:   len(t.Services),
		}
		summaries = append(summaries, ts)
	}
	return summaries
}

// formatUnixMs converts a Unix millisecond timestamp to RFC3339.
func formatUnixMs(ms int64) string {
	if ms == 0 {
		return ""
	}
	return time.UnixMilli(ms).Format(time.RFC3339)
}

// --- Tempo API response types ---

type tempoSearchResponse struct {
	Traces []tempoTrace `json:"traces"`
}

type tempoTrace struct {
	TraceID        string   `json:"traceID"`
	RootServiceName string  `json:"rootServiceName"`
	DurationMs     int64    `json:"durationMs"`
	SpanCount      int      `json:"spanCount"`
	StartTime      int64    `json:"startTime"` // Unix ms
	Services       []string `json:"services"`
}
