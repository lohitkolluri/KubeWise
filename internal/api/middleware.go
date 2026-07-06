package api

import (
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

func withMiddleware(next http.Handler, apiToken string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if apiToken != "" && r.URL.Path != "/health" {
			auth := r.Header.Get("Authorization")
			if !strings.HasPrefix(auth, "Bearer ") || strings.TrimPrefix(auth, "Bearer ") != apiToken {
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
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(data); err != nil {
		log.Printf("api: json encode error: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func decodeJSON(r *http.Request, dst any) error {
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
