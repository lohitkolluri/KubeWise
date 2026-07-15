package api

import (
	"crypto/subtle"
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

const maxRequestBodyBytes = 1 << 20 // 1 MiB

// Default is no CORS header (safer for localhost port-forward).
const defaultCORSOrigin = ""

// Rate limiting defaults.
const rateLimitPerMinute = 60
const rateLimitBurst = 10
const publicRateLimitPerMinute = 5
const publicRateLimitBurst = 5

var securityHeaders = map[string]string{
	"X-Content-Type-Options": "nosniff",
	"X-Frame-Options":        "DENY",
	"Referrer-Policy":        "strict-origin-when-cross-origin",
	"X-XSS-Protection":       "0", // discontinued but still scanned by some auditors
}

type middlewareConfig struct {
	apiToken          string
	corsOrigin        string
	requireToken      bool
	rateLimiter       *rateLimiter
	publicRateLimiter *rateLimiter
}

func (c middlewareConfig) rateLimitOrDefault() *rateLimiter {
	if c.rateLimiter != nil {
		return c.rateLimiter
	}
	return newRateLimiter(rateLimitPerMinute, rateLimitBurst)
}

func (c middlewareConfig) publicRateLimitOrDefault() *rateLimiter {
	if c.publicRateLimiter != nil {
		return c.publicRateLimiter
	}
	return newRateLimiter(publicRateLimitPerMinute, publicRateLimitBurst)
}

const rateLimiterMaxEntries = 10_000

// rateLimiter is a per-IP token bucket for API rate limiting.
type rateLimiter struct {
	mu         sync.Mutex
	buckets    map[string]*bucket
	rate       int
	burst      int
	lastGC     time.Time
	maxEntries int
}

type bucket struct {
	tokens   float64
	lastFill time.Time
}

func newRateLimiter(rate, burst int) *rateLimiter {
	return &rateLimiter{
		buckets:    make(map[string]*bucket),
		rate:       rate,
		burst:      burst,
		lastGC:     time.Now(),
		maxEntries: rateLimiterMaxEntries,
	}
}

func (rl *rateLimiter) allow(ip string) bool {
	rl.mu.Lock()

	now := time.Now()
	b, ok := rl.buckets[ip]
	if !ok {
		if len(rl.buckets) >= rl.maxEntries {
			rl.mu.Unlock()
			return false
		}
		b = &bucket{tokens: float64(rl.burst), lastFill: now}
		rl.buckets[ip] = b
	}

	elapsed := now.Sub(b.lastFill).Seconds()
	b.tokens += elapsed * float64(rl.rate)
	if b.tokens > float64(rl.burst) {
		b.tokens = float64(rl.burst)
	}
	b.lastFill = now

	if b.tokens < 1 {
		rl.mu.Unlock()
		return false
	}
	b.tokens--
	rl.mu.Unlock()

	// GC stale entries every 5 minutes using a snapshot pattern:
	// snapshot the map under lock, then iterate outside the lock to
	// avoid blocking concurrent requests during full-map traversal.
	if time.Since(rl.lastGC) > 5*time.Minute {
		rl.mu.Lock()
		snapshot := make(map[string]*bucket, len(rl.buckets))
		for k, v := range rl.buckets {
			snapshot[k] = v
		}
		rl.lastGC = time.Now()
		rl.mu.Unlock()

		cutoff := now.Add(-10 * time.Minute)
		var stale []string
		for k, b := range snapshot {
			if b.lastFill.Before(cutoff) {
				stale = append(stale, k)
			}
		}
		if len(stale) > 0 {
			rl.mu.Lock()
			for _, k := range stale {
				delete(rl.buckets, k)
			}
			rl.mu.Unlock()
		}
	}

	return true
}

func publicPath(path string) bool {
	switch path {
	case "/health", "/readyz", "/metrics", "/status", "/api/v1/auth":
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
		if origin := corsOriginForRequest(cfg.corsOrigin, r); origin != "" {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Vary", "Origin")
			// Minimal CORS support for browser clients.
			if origin == "*" {
				w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
			} else {
				w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
			}
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}

		// Rate limit by IP — public paths (auth, health) get a stricter limiter.
		if publicPath(r.URL.Path) {
			if !cfg.publicRateLimitOrDefault().allow(clientIP(r)) {
				w.Header().Set("Retry-After", "1")
				writeError(w, http.StatusTooManyRequests, "rate limit exceeded")
				return
			}
		} else {
			if !cfg.rateLimitOrDefault().allow(clientIP(r)) {
				w.Header().Set("Retry-After", "1")
				writeError(w, http.StatusTooManyRequests, "rate limit exceeded")
				return
			}
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
		slog.Info("api: request", "method", r.Method, "path", r.URL.Path, "status", lw.status, "duration", time.Since(start))
	})
}

// clientIP extracts the client IP from a request, checking X-Forwarded-For
// and X-Real-IP headers before falling back to RemoteAddr.
func clientIP(r *http.Request) string {
	if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
		if idx := strings.IndexByte(fwd, ','); idx >= 0 {
			return strings.TrimSpace(fwd[:idx])
		}
		return strings.TrimSpace(fwd)
	}
	if realIP := r.Header.Get("X-Real-IP"); realIP != "" {
		return strings.TrimSpace(realIP)
	}
	ip := r.RemoteAddr
	if idx := strings.LastIndex(ip, ":"); idx >= 0 {
		ip = ip[:idx]
	}
	return ip
}

func secureCompare(got, want string) bool {
	if got == "" || want == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

// corsOriginForRequest resolves the Access-Control-Allow-Origin header.
// Supports a single origin, comma-separated allowlist, or "*" wildcard.
func corsOriginForRequest(configured string, r *http.Request) string {
	configured = strings.TrimSpace(configured)
	if configured == "" {
		return ""
	}
	if configured == "*" {
		return "*"
	}
	origins := strings.Split(configured, ",")
	reqOrigin := strings.TrimSpace(r.Header.Get("Origin"))
	if reqOrigin != "" {
		for _, origin := range origins {
			if strings.TrimSpace(origin) == reqOrigin {
				return reqOrigin
			}
		}
		return ""
	}
	if len(origins) == 1 {
		return strings.TrimSpace(origins[0])
	}
	return ""
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
		slog.Error("api: json encode error", "error", err)
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
