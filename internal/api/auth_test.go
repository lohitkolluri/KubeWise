package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAuthMiddleware_RejectsWithoutToken(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/stats", func(w http.ResponseWriter, r *http.Request) {
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
	mux.HandleFunc("GET /api/v1/stats", func(w http.ResponseWriter, r *http.Request) {
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
			mux.HandleFunc("GET "+path, func(w http.ResponseWriter, r *http.Request) {
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
	mux.HandleFunc("GET /api/v1/stats", func(w http.ResponseWriter, r *http.Request) {
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
