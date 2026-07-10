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

var lokiHTTPClient = &http.Client{Timeout: 5 * time.Second}

const logErrorFilter = ` |~ "(?i)error|panic|oom|fail|exception|crash|killed|evicted|backoff"`

// FetchLogSnippets queries Loki for recent log lines matching the namespace/pod.
// Returns nil when Loki is not configured (empty URL).
func FetchLogSnippets(ctx context.Context, lokiURL, namespace, pod string, since time.Duration) ([]LogSnippet, error) {
	return fetchLogSnippets(ctx, lokiURL, namespace, pod, since)
}

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

	start := time.Now().Add(-since)
	end := time.Now()

	var lastErr error
	for _, logql := range buildLogQLCandidates(namespace, pod) {
		snippets, err := queryLokiRange(ctx, lokiURL, logql, start, end, 50)
		if err != nil {
			lastErr = err
			continue
		}
		if len(snippets) > 0 {
			return snippets, nil
		}
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, nil
}

func queryLokiRange(ctx context.Context, lokiURL, logql string, start, end time.Time, limit int) ([]LogSnippet, error) {
	u, err := url.Parse(lokiURL + "/loki/api/v1/query_range")
	if err != nil {
		return nil, fmt.Errorf("parse loki URL: %w", err)
	}
	q := u.Query()
	q.Set("query", logql)
	q.Set("start", fmt.Sprintf("%d", start.UnixNano()))
	q.Set("end", fmt.Sprintf("%d", end.UnixNano()))
	q.Set("limit", fmt.Sprintf("%d", limit))
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("create loki request: %w", err)
	}

	resp, err := lokiHTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("query loki: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

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

// buildLogQLCandidates returns LogQL queries from most specific to most permissive.
// Alloy labels streams with namespace+pod; legacy/canary streams may only have pod.
func buildLogQLCandidates(namespace, pod string) []string {
	var out []string
	if namespace != "" && pod != "" {
		out = append(out, fmt.Sprintf(`{namespace=%q, pod=%q}`, namespace, pod)+logErrorFilter)
	}
	if pod != "" {
		out = append(out, fmt.Sprintf(`{pod=%q}`, pod)+logErrorFilter)
	}
	if namespace != "" && pod == "" {
		out = append(out, fmt.Sprintf(`{namespace=%q}`, namespace)+logErrorFilter)
	}
	return out
}

// buildLogQL constructs the primary LogQL query (first candidate).
func buildLogQL(namespace, pod string) string {
	candidates := buildLogQLCandidates(namespace, pod)
	if len(candidates) == 0 {
		return ""
	}
	return candidates[0]
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
	Status string        `json:"status"`
	Data   lokiQueryData `json:"data"`
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
