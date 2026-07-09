package api

import (
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const maxRequestBodyBytes = 1 << 20 // 1 MiB

// Default is no CORS header (safer for localhost port-forward).
const defaultCORSOrigin = ""

var securityHeaders = map[string]string{
	"X-Content-Type-Options": "nosniff",
	"X-Frame-Options":        "DENY",
	"Referrer-Policy":        "strict-origin-when-cross-origin",
	"X-XSS-Protection":       "0", // discontinued but still scanned by some auditors
}

type middlewareConfig struct {
	apiToken     string
	corsOrigin   string
	requireToken bool
}

func publicPath(path string) bool {
	switch path {
	case "/health", "/readyz", "/metrics", "/status":
		return true
	default:
		return false
	}
}

func withMiddleware(next http.Handler, cfg middlewareConfig) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for k, v := range securityHeaders {
			w.Header().Set(k, v)
		}
		if cfg.corsOrigin != "" {
			w.Header().Set("Access-Control-Allow-Origin", cfg.corsOrigin)
			w.Header().Set("Vary", "Origin")
		}

		needAuth := !publicPath(r.URL.Path) && (cfg.requireToken || cfg.apiToken != "")
		if needAuth {
			if cfg.apiToken == "" {
				writeError(w, http.StatusUnauthorized, "unauthorized")
				return
			}
			auth := r.Header.Get("Authorization")
			if !strings.HasPrefix(auth, "Bearer ") || !secureCompare(strings.TrimPrefix(auth, "Bearer "), cfg.apiToken) {
				writeError(w, http.StatusUnauthorized, "unauthorized")
				return
			}
		}

		start := time.Now()
		lw := &logWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(lw, r)
		log.Printf("%s %s %d %s", r.Method, r.URL.Path, lw.status, time.Since(start))
	})
}

func secureCompare(got, want string) bool {
	if got == "" || want == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

type logWriter struct {
	http.ResponseWriter
	status int
}

func (w *logWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func writeJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(data); err != nil {
		log.Printf("api: json encode error: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(dst)
}

func parseLimit(r *http.Request, defaultLimit, maxLimit int) (int, error) {
	l := r.URL.Query().Get("limit")
	if l == "" {
		return defaultLimit, nil
	}
	parsed, err := strconv.Atoi(l)
	if err != nil {
		return 0, err
	}
	if parsed <= 0 || parsed > maxLimit {
		return 0, strconv.ErrRange
	}
	return parsed, nil
}
