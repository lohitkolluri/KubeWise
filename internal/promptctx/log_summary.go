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

// fetchLogSnippets queries Loki for recent log lines matching the namespace
// and pod. Returns nil gracefully when lokiURL is empty (Loki not configured).
// Queries the last 15 minutes, max 50 lines, truncated to 500 chars per line.
func fetchLogSnippets(ctx context.Context, lokiURL, namespace, pod string, since time.Duration) ([]LogSnippet, error) {
	if lokiURL == "" {
		return nil, nil
	}
	if namespace == "" && pod == "" {
		return nil, nil
	}

	// Build LogQL: {namespace="...", pod="..."} |= "error|panic|OOM|fail"
	logql := buildLogQL(namespace, pod)
	start := time.Now().Add(-since)
	end := time.Now()

	u, err := url.Parse(lokiURL + "/loki/api/v1/query_range")
	if err != nil {
		return nil, fmt.Errorf("parse loki URL: %w", err)
	}
	q := u.Query()
	q.Set("query", logql)
	q.Set("start", fmt.Sprintf("%d", start.UnixNano()))
	q.Set("end", fmt.Sprintf("%d", end.UnixNano()))
	q.Set("limit", "50")
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("create loki request: %w", err)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("query loki: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return nil, fmt.Errorf("loki returned %d: %s", resp.StatusCode, string(body))
	}

	var result lokiQueryResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode loki response: %w", err)
	}

	return flattenLogSnippets(result), nil
}

// buildLogQL constructs a LogQL query filtering by namespace and/or pod
// with common error keywords.
func buildLogQL(namespace, pod string) string {
	var labelFilters []string
	if namespace != "" {
		labelFilters = append(labelFilters, fmt.Sprintf(`namespace=%q`, namespace))
	}
	if pod != "" {
		labelFilters = append(labelFilters, fmt.Sprintf(`pod=%q`, pod))
	}
	stream := fmt.Sprintf("{%s}", joinStrings(labelFilters, ", "))
	return stream + ` |~ "(?i)error|panic|oom|fail|exception|crash|killed|evicted|backoff"`
}

// joinStrings joins strings with sep, skipping empty entries.
func joinStrings(parts []string, sep string) string {
	result := ""
	for i, p := range parts {
		if i > 0 {
			result += sep
		}
		result += p
	}
	return result
}

// flattenLogSnippets extracts log lines from the Loki query response.
func flattenLogSnippets(resp lokiQueryResponse) []LogSnippet {
	if resp.Status != "success" {
		return nil
	}
	if len(resp.Data.Result) == 0 {
		return nil
	}

	var snippets []LogSnippet
	for _, stream := range resp.Data.Result {
		container := stream.Stream.Container
		for _, entry := range stream.Values {
			if len(entry) < 2 {
				continue
			}
			line := truncateLine(entry[1], 500)
			snippets = append(snippets, LogSnippet{
				Timestamp: formatLokiTimestamp(entry[0]),
				Line:      line,
				Container: container,
			})
			if len(snippets) >= 50 {
				return snippets
			}
		}
	}
	return snippets
}

// formatLokiTimestamp converts a Loki nanosecond timestamp string to RFC3339.
func formatLokiTimestamp(nano string) string {
	if nano == "" {
		return ""
	}
	var nanos int64
	_, err := fmt.Sscanf(nano, "%d", &nanos)
	if err != nil {
		return nano
	}
	return time.Unix(0, nanos).Format(time.RFC3339)
}

// truncateLine truncates a log line to maxLen runes.
func truncateLine(line string, maxLen int) string {
	runes := []rune(line)
	if len(runes) <= maxLen {
		return line
	}
	return string(runes[:maxLen]) + "..."
}

// --- Loki API response types ---

type lokiQueryResponse struct {
	Status string           `json:"status"`
	Data   lokiQueryData    `json:"data"`
}

type lokiQueryData struct {
	Result []lokiStreamResult `json:"result"`
}

type lokiStreamResult struct {
	Stream lokiStreamLabels `json:"stream"`
	Values [][]string       `json:"values"`
}

type lokiStreamLabels struct {
	Container string `json:"container"`
}
