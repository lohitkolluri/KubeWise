package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/term"
)

const defaultAgentURL = "http://localhost:8080"

var (
	agentURL    string
	apiToken    string
	httpTimeout int

	passwordAttempted bool   // prevents repeated password prompts per session
	cachedPassword    string // password from --pass flag, set by installCmd

	// sharedTransport holds a persistent http.Transport with connection pooling
	// so TCP connections are reused across requests instead of being closed.
	sharedTransport     http.RoundTripper
	sharedTransportOnce sync.Once
)

func initSharedTransport() http.RoundTripper {
	sharedTransportOnce.Do(func() {
		sharedTransport = &http.Transport{
			MaxIdleConns:    10,
			IdleConnTimeout: 90 * time.Second,
		}
	})
	return sharedTransport
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
	timeout := httpTimeout
	if timeout <= 0 {
		timeout = 15
	}
	return &http.Client{
		Timeout:   time.Duration(timeout) * time.Second,
		Transport: initSharedTransport(),
	}
}

func agentRequest(ctx context.Context, method, path string, body any) ([]byte, int, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, 0, fmt.Errorf("marshal body: %w", err)
		}
		reader = bytes.NewReader(data)
	}
	if ctx == nil {
		ctx = context.Background()
	}
	req, err := http.NewRequestWithContext(ctx, method, resolveAgentURL()+path, reader)
	if err != nil {
		return nil, 0, fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	token, authErr := resolveRequestToken(ctx)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := agentHTTPClient().Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("connecting to agent: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("reading response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		msg := strings.TrimSpace(string(respBody))
		// Agent often returns structured JSON errors: {"error":"..."}.
		// Prefer a clean message to avoid printing raw JSON blobs in CLI output.
		var apiErr struct {
			Error   string `json:"error"`
			Message string `json:"message"`
		}
		if err := json.Unmarshal(respBody, &apiErr); err == nil {
			if apiErr.Error != "" {
				msg = apiErr.Error
			} else if apiErr.Message != "" {
				msg = apiErr.Message
			}
		}
		// If auth clearly failed and we know why, surface the underlying cause
		// instead of a generic 401 (e.g. wrong password, agent missing token).
		if resp.StatusCode == http.StatusUnauthorized && authErr != nil {
			return respBody, resp.StatusCode, fmt.Errorf("authentication failed: %w", authErr)
		}
		if resp.StatusCode == http.StatusMethodNotAllowed {
			return respBody, resp.StatusCode, fmt.Errorf("agent returned 405 method not allowed — rebuild and redeploy the agent, or check the API path: %s", msg)
		}
		return respBody, resp.StatusCode, fmt.Errorf("agent returned status %d: %s", resp.StatusCode, msg)
	}
	return respBody, resp.StatusCode, nil
}

// resolveRequestToken picks the API token for a request. An explicit --pass
// takes precedence over any saved profile/env token, so re-running with
// --pass always re-authenticates even when a stale token is cached. The
// returned error is non-nil only when no token could be obtained; it is
// surfaced by the caller on a 401 so the user sees the real cause.
func resolveRequestToken(ctx context.Context) (string, error) {
	var authErr error
	if cachedPassword != "" && !passwordAttempted {
		passwordAttempted = true
		tok, err := tryPasswordAuth(ctx, resolveAgentURL())
		switch {
		case err != nil:
			authErr = err
		case tok != "":
			apiToken = tok
			_ = saveAPIToken(tok)
			return tok, nil
		default:
			// tryPasswordAuth returned no error and no token: the agent
			// answered 404 (password auth not configured). Record a clear
			// diagnostic so a later 401 surfaces the real cause.
			authErr = fmt.Errorf("agent has no password authentication configured — re-run install with --pass or run 'kwctl agent restart'")
		}
	}
	if apiToken != "" {
		// Preserve authErr so a 401 from a stale token still surfaces
		// the --pass failure reason to the user.
		return apiToken, authErr
	}
	if t := os.Getenv("KUBEWISE_API_TOKEN"); t != "" {
		return t, authErr
	}
	if !passwordAttempted {
		passwordAttempted = true
		tok, err := tryPasswordAuth(ctx, resolveAgentURL())
		if err != nil {
			authErr = err
		} else if tok != "" {
			apiToken = tok
			_ = saveAPIToken(tok)
			return tok, nil
		}
	}
	return "", authErr
}

func agentGet(ctx context.Context, path string) ([]byte, int, error) {
	return agentRequest(ctx, http.MethodGet, path, nil)
}

// agentWrite tries common write methods; returns a deploy hint when the agent is too old.
func agentWrite(ctx context.Context, path string, body any) ([]byte, int, error) {
	var lastBody []byte
	var lastCode int
	var lastErr error
	for _, method := range []string{http.MethodPut, http.MethodPost, http.MethodPatch} {
		lastBody, lastCode, lastErr = agentRequest(ctx, method, path, body)
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

// tryPasswordAuth attempts to get an API token by exchanging the password
// via the agent's POST /api/v1/auth endpoint. Returns empty string on failure
// (caller should fall back to the usual auth failure path).
func tryPasswordAuth(ctx context.Context, baseURL string) (string, error) {
	password := cachedPassword
	if password == "" {
		fmt.Fprint(os.Stderr, "Enter agent password: ")
		pw, err := term.ReadPassword(int(syscall.Stdin))
		fmt.Fprintln(os.Stderr)
		if err != nil {
			return "", err
		}
		password = string(pw)
	}
	if password == "" {
		return "", nil
	}

	body, err := json.Marshal(map[string]string{"password": password})
	if err != nil {
		return "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/api/v1/auth", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := agentHTTPClient().Do(req)
	if err != nil {
		return "", err
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode == http.StatusNotFound {
		// Agent doesn't have password auth configured, skip silently
		return "", nil
	}
	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(resp.Body)
		msg := strings.TrimSpace(string(respBody))
		// Try to extract a cleaner error message from JSON
		var apiErr struct {
			Error   string `json:"error"`
			Message string `json:"message"`
		}
		if json.Unmarshal(respBody, &apiErr) == nil {
			if apiErr.Error != "" {
				msg = apiErr.Error
			} else if apiErr.Message != "" {
				msg = apiErr.Message
			}
		}
		return "", fmt.Errorf("password auth failed: %s", msg)
	}

	var result struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("decoding auth response: %w", err)
	}
	return result.Token, nil
}
