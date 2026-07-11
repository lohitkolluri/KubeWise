// Package api provides the HTTP API server for the KubeWise agent.
package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"golang.org/x/crypto/bcrypt"
)

func TestAuthMiddleware_RejectsWithoutToken(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/stats", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := withMiddleware(mux, middlewareConfig{apiToken: "secret-token", corsOrigin: "*"})
	ts := httptest.NewServer(handler)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/api/v1/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", resp.StatusCode)
	}
}

func TestAuthMiddleware_AcceptsBearerToken(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/stats", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := withMiddleware(mux, middlewareConfig{apiToken: "secret-token", corsOrigin: "*"})
	ts := httptest.NewServer(handler)
	defer ts.Close()

	req, _ := http.NewRequest(http.MethodGet, ts.URL+"/api/v1/stats", nil)
	req.Header.Set("Authorization", "Bearer secret-token")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

func TestAuthMiddleware_HealthBypassesAuth(t *testing.T) {
	for _, path := range []string{"/health", "/readyz", "/metrics", "/status"} {
		t.Run(path, func(t *testing.T) {
			mux := http.NewServeMux()
			mux.HandleFunc("GET "+path, func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
			})

			handler := withMiddleware(mux, middlewareConfig{apiToken: "secret-token", corsOrigin: "*", requireToken: true})
			ts := httptest.NewServer(handler)
			defer ts.Close()

			resp, err := http.Get(ts.URL + path)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("%s: expected 200, got %d", path, resp.StatusCode)
			}
		})
	}
}

func TestAuthMiddleware_RequireTokenFailsClosedWhenTokenMissing(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/stats", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := withMiddleware(mux, middlewareConfig{apiToken: "", corsOrigin: "", requireToken: true})
	ts := httptest.NewServer(handler)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/api/v1/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", resp.StatusCode)
	}
}

func TestSecureCompare(t *testing.T) {
	if secureCompare("abc", "abc") != true {
		t.Fatal("expected match")
	}
	if secureCompare("abc", "abd") != false {
		t.Fatal("expected mismatch")
	}
	if secureCompare("", "abc") != false {
		t.Fatal("expected false for empty got")
	}
}

func TestHandleAuth_ValidPassword(t *testing.T) {
	s := &Server{apiToken: "test-server-token"}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/auth", s.handleAuth)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	hash, err := bcrypt.GenerateFromPassword([]byte("hunter2"), bcrypt.DefaultCost)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CLIENT_PASSWORD_HASH", string(hash))

	body := `{"password":"hunter2"}`
	resp, err := http.Post(ts.URL+"/api/v1/auth", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var result struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	if result.Token != "test-server-token" {
		t.Fatalf("expected token 'test-server-token', got %q", result.Token)
	}
}

func TestHandleAuth_InvalidPassword(t *testing.T) {
	s := &Server{apiToken: "test-server-token"}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/auth", s.handleAuth)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	hash, err := bcrypt.GenerateFromPassword([]byte("correct-password"), bcrypt.DefaultCost)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CLIENT_PASSWORD_HASH", string(hash))

	body := `{"password":"wrong-password"}`
	resp, err := http.Post(ts.URL+"/api/v1/auth", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", resp.StatusCode)
	}
}

func TestHandleAuth_NotConfigured(t *testing.T) {
	s := &Server{apiToken: "test-server-token"}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/auth", s.handleAuth)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	t.Setenv("CLIENT_PASSWORD_HASH", "")

	body := `{"password":"hunter2"}`
	resp, err := http.Post(ts.URL+"/api/v1/auth", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", resp.StatusCode)
	}
}

func TestHandleAuth_NoServerToken(t *testing.T) {
	s := &Server{apiToken: ""}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/auth", s.handleAuth)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	hash, err := bcrypt.GenerateFromPassword([]byte("hunter2"), bcrypt.DefaultCost)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CLIENT_PASSWORD_HASH", string(hash))

	body := `{"password":"hunter2"}`
	resp, err := http.Post(ts.URL+"/api/v1/auth", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("expected 500, got %d", resp.StatusCode)
	}
}

func TestHandleAuth_InvalidBody(t *testing.T) {
	s := &Server{apiToken: "test-server-token"}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/auth", s.handleAuth)
	ts := httptest.NewServer(mux)
	defer ts.Close()

	t.Setenv("CLIENT_PASSWORD_HASH", "some-hash")

	body := `not-json`
	resp, err := http.Post(ts.URL+"/api/v1/auth", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", resp.StatusCode)
	}
}

func TestAuthMiddleware_PublicPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/auth", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /api/v1/stats", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := withMiddleware(mux, middlewareConfig{apiToken: "secret", corsOrigin: "*", requireToken: true})
	ts := httptest.NewServer(handler)
	defer ts.Close()

	resp, err := http.Post(ts.URL+"/api/v1/auth", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("/api/v1/auth: expected 200, got %d", resp.StatusCode)
	}

	resp, err = http.Get(ts.URL + "/api/v1/stats")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("/api/v1/stats: expected 401, got %d", resp.StatusCode)
	}
}

func TestCORSOriginForRequest(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/stats", nil)
	req.Header.Set("Origin", "http://localhost:3000")

	if got := corsOriginForRequest("*", req); got != "*" {
		t.Fatalf("wildcard: got %q", got)
	}
	if got := corsOriginForRequest("http://localhost:3000,http://127.0.0.1:3000", req); got != "http://localhost:3000" {
		t.Fatalf("allowlist match: got %q", got)
	}
	req.Header.Set("Origin", "http://evil.example")
	if got := corsOriginForRequest("http://localhost:3000", req); got != "" {
		t.Fatalf("unexpected origin for disallowed host: %q", got)
	}
	if got := corsOriginForRequest("*", req); got != "*" {
		t.Fatalf("wildcard: got %q", got)
	}
	req.Header.Del("Origin")
	if got := corsOriginForRequest("http://localhost:3000", req); got != "http://localhost:3000" {
		t.Fatalf("single configured origin without request Origin: got %q", got)
	}
}
