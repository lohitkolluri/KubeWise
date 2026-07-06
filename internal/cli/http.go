package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

const defaultAgentURL = "http://localhost:8080"

var (
	agentURL    string
	apiToken    string
	httpTimeout int
)

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
	timeout := httpTimeout
	if timeout <= 0 {
		timeout = 15
	}
	return &http.Client{Timeout: time.Duration(timeout) * time.Second}
}

func agentRequest(method, path string, body any) ([]byte, int, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, 0, fmt.Errorf("marshal body: %w", err)
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, resolveAgentURL()+path, reader)
	if err != nil {
		return nil, 0, fmt.Errorf("building request: %w", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	token := apiToken
	if token == "" {
		token = os.Getenv("KUBEWISE_API_TOKEN")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := agentHTTPClient().Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("connecting to agent: %w", err)
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("reading response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		msg := string(respBody)
		if resp.StatusCode == http.StatusMethodNotAllowed {
			return respBody, resp.StatusCode, fmt.Errorf("agent returned 405 method not allowed — rebuild and redeploy the agent, or check the API path: %s", msg)
		}
		return respBody, resp.StatusCode, fmt.Errorf("agent returned status %d: %s", resp.StatusCode, msg)
	}
	return respBody, resp.StatusCode, nil
}

func agentGet(path string) ([]byte, int, error) {
	return agentRequest(http.MethodGet, path, nil)
}

// agentWrite tries common write methods; returns a deploy hint when the agent is too old.
func agentWrite(path string, body any) ([]byte, int, error) {
	var lastBody []byte
	var lastCode int
	var lastErr error
	for _, method := range []string{http.MethodPut, http.MethodPost, http.MethodPatch} {
		lastBody, lastCode, lastErr = agentRequest(method, path, body)
		if lastErr == nil {
			return lastBody, lastCode, nil
		}
		if lastCode != http.StatusMethodNotAllowed && lastCode != http.StatusNotFound {
			return lastBody, lastCode, lastErr
		}
	}
	if lastCode == http.StatusMethodNotAllowed || lastCode == http.StatusNotFound {
		hint := strings.TrimSpace(string(lastBody))
		if hint == "" {
			hint = "endpoint missing on running agent"
		}
		return lastBody, lastCode, fmt.Errorf("agent returned %d on %s — redeploy with: bash hack/deploy-dev.sh (%s)", lastCode, path, hint)
	}
	return lastBody, lastCode, lastErr
}

func agentPut(path string, body any) ([]byte, int, error) {
	return agentWrite(path, body)
}
