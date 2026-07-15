package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// saveResetGlobals captures the current values of all package-level vars
// that http.go uses and restores them on cleanup. Returns a restore func.
func saveResetGlobals(t *testing.T) func() {
	t.Helper()
	oldAgentURL := agentURL
	oldAPIToken := apiToken
	oldHTTPTimeout := httpTimeout
	oldCachedPW := cachedPassword
	oldPWAttempted := passwordAttempted

	agentURL = ""
	apiToken = ""
	httpTimeout = 0
	cachedPassword = ""
	passwordAttempted = false

	return func() {
		agentURL = oldAgentURL
		apiToken = oldAPIToken
		httpTimeout = oldHTTPTimeout
		cachedPassword = oldCachedPW
		passwordAttempted = oldPWAttempted
	}
}

func TestResolveAgentURL_Global(t *testing.T) {
	defer saveResetGlobals(t)()
	agentURL = "http://override:8080"
	if got := resolveAgentURL(); got != "http://override:8080" {
		t.Fatalf("expected override URL, got %q", got)
	}
}

func TestResolveAgentURL_EnvVar(t *testing.T) {
	defer saveResetGlobals(t)()
	t.Setenv("KUBEWISE_AGENT_URL", "http://from-env:9999")
	if got := resolveAgentURL(); got != "http://from-env:9999" {
		t.Fatalf("expected env var URL, got %q", got)
	}
}

func TestResolveAgentURL_Default(t *testing.T) {
	defer saveResetGlobals(t)()
	if got := resolveAgentURL(); got != "http://localhost:8080" {
		t.Fatalf("expected default URL, got %q", got)
	}
}

func TestResolveAgentURL_GlobalOverridesEnv(t *testing.T) {
	defer saveResetGlobals(t)()
	t.Setenv("KUBEWISE_AGENT_URL", "http://env:8080")
	agentURL = "http://global:8080"
	if got := resolveAgentURL(); got != "http://global:8080" {
		t.Fatalf("expected global (%q) over env, got %q", agentURL, got)
	}
}

func TestAgentHTTPClient_DefaultTimeout(t *testing.T) {
	defer saveResetGlobals(t)()
	httpTimeout = 0
	c := agentHTTPClient()
	if c.Timeout.String() != "15s" {
		t.Fatalf("expected 15s timeout, got %v", c.Timeout)
	}
}

func TestAgentHTTPClient_CustomTimeout(t *testing.T) {
	defer saveResetGlobals(t)()
	httpTimeout = 30
	c := agentHTTPClient()
	if c.Timeout.String() != "30s" {
		t.Fatalf("expected 30s timeout, got %v", c.Timeout)
	}
}

// --- tryPasswordAuth tests ---

func TestTryPasswordAuth_ReturnsToken(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("expected POST, got %s", r.Method)
		}
		if r.URL.Path != "/api/v1/auth" {
			t.Fatalf("expected /api/v1/auth, got %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"token":"exchanged-token-abc"}`)
	}))
	defer ts.Close()

	cachedPassword = "test-password"
	token, err := tryPasswordAuth(context.Background(), ts.URL)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != "exchanged-token-abc" {
		t.Fatalf("expected 'exchanged-token-abc', got %q", token)
	}
}

func TestTryPasswordAuth_NotConfigured(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer ts.Close()

	cachedPassword = "test-password"
	token, err := tryPasswordAuth(context.Background(), ts.URL)
	if err != nil {
		t.Fatalf("expected nil error for 404, got: %v", err)
	}
	if token != "" {
		t.Fatalf("expected empty token for 404, got %q", token)
	}
}

func TestResolveRequestToken_PassButAgentNotConfigured(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer ts.Close()

	agentURL = ts.URL
	cachedPassword = "test-password"
	tok, err := resolveRequestToken(context.Background())
	if err == nil {
		t.Fatal("expected error when agent has no password auth configured")
	}
	if tok != "" {
		t.Fatalf("expected empty token, got %q", tok)
	}
	if !strings.Contains(err.Error(), "no password authentication configured") {
		t.Fatalf("expected diagnostic about missing password auth, got: %v", err)
	}
}

func TestTryPasswordAuth_WrongPassword(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `{"error":"invalid password"}`)
	}))
	defer ts.Close()

	cachedPassword = "wrong-password"
	token, err := tryPasswordAuth(context.Background(), ts.URL)
	if err == nil {
		t.Fatal("expected error for 401")
	}
	if token != "" {
		t.Fatalf("expected empty token on error, got %q", token)
	}
}

func TestTryPasswordAuth_EmptyCachedPassword(t *testing.T) {
	defer saveResetGlobals(t)()
	cachedPassword = ""
	// When cachedPassword is empty, tryPasswordAuth tries to prompt on stdin.
	// In a test environment (non-TTY stdin), this returns an error.
	token, err := tryPasswordAuth(context.Background(), "http://should-not-be-called")
	if err == nil {
		t.Fatal("expected error when stdin is not a terminal")
	}
	if token != "" {
		t.Fatalf("expected empty token on error, got %q", token)
	}
}

func TestAgentRequest_SkipsPasswordAuthWhenAlreadyAttempted(t *testing.T) {
	defer saveResetGlobals(t)()

	passwordAttempted = true
	cachedPassword = ""

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Fatalf("expected no Authorization header")
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestTryPasswordAuth_ConnectionError(t *testing.T) {
	defer saveResetGlobals(t)()
	cachedPassword = "test-password"
	token, err := tryPasswordAuth(context.Background(), "http://127.0.0.1:1")
	if err == nil {
		t.Fatal("expected connection error")
	}
	if token != "" {
		t.Fatalf("expected empty token on error, got %q", token)
	}
}

// --- agentRequest tests ---

func TestAgentRequest_Get(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("expected GET, got %s", r.Method)
		}
		if r.URL.Path != "/api/v1/test" {
			t.Fatalf("expected /api/v1/test, got %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "" {
			t.Fatalf("expected no auth header, got %q", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	body, code, err := agentRequest(context.Background(), http.MethodGet, "/api/v1/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if code != http.StatusOK {
		t.Fatalf("expected 200, got %d", code)
	}
	var resp struct {
		Ok bool `json:"ok"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		t.Fatal(err)
	}
	if !resp.Ok {
		t.Fatal("expected ok=true")
	}
}

func TestAgentRequest_WithToken(t *testing.T) {
	defer saveResetGlobals(t)()
	apiToken = "static-token-xyz"

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer static-token-xyz" {
			t.Fatalf("expected Bearer static-token-xyz, got %q", r.Header.Get("Authorization"))
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestAgentRequest_TokenFromEnv(t *testing.T) {
	defer saveResetGlobals(t)()
	t.Setenv("KUBEWISE_API_TOKEN", "env-token-abc")

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer env-token-abc" {
			t.Fatalf("expected Bearer env-token-abc, got %q", r.Header.Get("Authorization"))
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

// apiToken takes priority over KUBEWISE_API_TOKEN env var
func TestAgentRequest_TokenPriority(t *testing.T) {
	defer saveResetGlobals(t)()
	apiToken = "global-token"
	t.Setenv("KUBEWISE_API_TOKEN", "env-token")

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer global-token" {
			t.Fatalf("expected Bearer global-token, got %q", r.Header.Get("Authorization"))
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestAgentRequest_POSTWithBody(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("expected POST, got %s", r.Method)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Fatalf("expected application/json, got %q", ct)
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	payload := map[string]string{"key": "value"}
	_, _, err := agentRequest(context.Background(), http.MethodPost, "/test", payload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestAgentRequest_ErrorResponse(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"error":"bad request"}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, code, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if code != http.StatusBadRequest {
		t.Fatalf("expected 400 status code, got %d", code)
	}
	if err == nil {
		t.Fatal("expected error for 400 response")
	}
}

func TestAgentRequest_405Error(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusMethodNotAllowed)
		fmt.Fprint(w, `{"error":"method not allowed"}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, code, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405 status code, got %d", code)
	}
	if err == nil {
		t.Fatal("expected error for 405 response")
	}
	if !strings.Contains(err.Error(), "rebuild and redeploy") {
		t.Fatalf("expected rebuild hint in error, got: %v", err)
	}
}

func TestAgentRequest_401SurfacesPassAuthErr(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/auth":
			w.WriteHeader(http.StatusNotFound)
		default:
			w.WriteHeader(http.StatusUnauthorized)
			fmt.Fprint(w, `{"error":"unauthorized"}`)
		}
	}))
	defer ts.Close()

	agentURL = ts.URL
	cachedPassword = "test-password"
	_, code, err := agentRequest(context.Background(), http.MethodGet, "/anomalies", nil)
	if code != http.StatusUnauthorized {
		t.Fatalf("expected 401 status code, got %d", code)
	}
	if err == nil {
		t.Fatal("expected error for 401 with --pass auth failure")
	}
	if !strings.Contains(err.Error(), "authentication failed") {
		t.Fatalf("expected 'authentication failed' prefix, got: %v", err)
	}
	if !strings.Contains(err.Error(), "no password authentication configured") {
		t.Fatalf("expected diagnostic about missing password auth, got: %v", err)
	}
}

func TestAgentRequest_EmptyPasswordSkipsAuth(t *testing.T) {
	defer saveResetGlobals(t)()
	cachedPassword = ""

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Fatalf("expected no auth header when no token/password set")
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestAgentRequest_BackgroundContext(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestAgentWrite_PutThenPostThenPatch(t *testing.T) {
	defer saveResetGlobals(t)()

	calls := 0
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method == http.MethodPut {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if r.Method == http.MethodPatch {
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, `{"success":true}`)
			return
		}
		t.Fatalf("unexpected method: %s", r.Method)
	}))
	defer ts.Close()

	agentURL = ts.URL
	body, code, err := agentWrite(context.Background(), "/test", map[string]string{"key": "val"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if code != http.StatusOK {
		t.Fatalf("expected 200, got %d", code)
	}
	if calls != 3 {
		t.Fatalf("expected 3 method attempts, got %d", calls)
	}
	var resp struct {
		Success bool `json:"success"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		t.Fatal(err)
	}
	if !resp.Success {
		t.Fatal("expected success=true")
	}
}

func TestAgentWrite_AllFail(t *testing.T) {
	defer saveResetGlobals(t)()

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusMethodNotAllowed)
		fmt.Fprint(w, `{"error":"nope"}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, code, err := agentWrite(context.Background(), "/test", nil)
	if code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", code)
	}
	if err == nil {
		t.Fatal("expected error when all methods fail")
	}
	if !strings.Contains(err.Error(), "redeploy") {
		t.Fatalf("expected deploy hint in error, got: %v", err)
	}
}

func TestAgentGet(t *testing.T) {
	defer saveResetGlobals(t)()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("expected GET, got %s", r.Method)
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	_, code, err := agentGet(context.Background(), "/test")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if code != http.StatusOK {
		t.Fatalf("expected 200, got %d", code)
	}
}

func TestAgentRequest_PasswordExchangeOnFirstCall(t *testing.T) {
	defer saveResetGlobals(t)()

	authCalled := false
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/auth" {
			authCalled = true
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, `{"token":"pw-exchanged-token"}`)
			return
		}
		if r.Header.Get("Authorization") != "Bearer pw-exchanged-token" {
			t.Fatalf("expected Bearer pw-exchanged-token, got %q", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	cachedPassword = "test-password"
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !authCalled {
		t.Fatal("expected password auth to be called on first request")
	}
}

func TestAgentRequest_PasswordExchangeOnlyOnce(t *testing.T) {
	defer saveResetGlobals(t)()

	authCount := 0
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/auth" {
			authCount++
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, `{"token":"pw-exchanged-token"}`)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer ts.Close()

	agentURL = ts.URL
	cachedPassword = "test-password"
	_, _, err := agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("first request: %v", err)
	}
	_, _, err = agentRequest(context.Background(), http.MethodGet, "/test", nil)
	if err != nil {
		t.Fatalf("second request: %v", err)
	}
	if authCount != 1 {
		t.Fatalf("expected password auth called once, got %d calls", authCount)
	}
}
