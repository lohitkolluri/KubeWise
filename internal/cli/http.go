package cli

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

const defaultAgentURL = "http://localhost:8080"

var agentURL string

func init() {
	rootCmd.PersistentFlags().StringVar(&agentURL, "agent-url", "", "agent base URL (overrides KUBEWISE_AGENT_URL)")
}

func resolveAgentURL() string {
	if agentURL != "" {
		return agentURL
	}
	if u := os.Getenv("KUBEWISE_AGENT_URL"); u != "" {
		return u
	}
	return defaultAgentURL
}

func agentHTTPClient() *http.Client {
	return &http.Client{Timeout: 15 * time.Second}
}

func agentGet(path string) ([]byte, int, error) {
	req, err := http.NewRequest(http.MethodGet, resolveAgentURL()+path, nil)
	if err != nil {
		return nil, 0, fmt.Errorf("building request: %w", err)
	}
	if token := os.Getenv("KUBEWISE_API_TOKEN"); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := agentHTTPClient().Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("connecting to agent: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("reading response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return body, resp.StatusCode, fmt.Errorf("agent returned status %d: %s", resp.StatusCode, string(body))
	}
	return body, resp.StatusCode, nil
}

func validateOutputFormat() error {
	switch outputFormat {
	case "table", "json", "yaml":
		return nil
	default:
		return fmt.Errorf("invalid output format %q: use table, json, or yaml", outputFormat)
	}
}
