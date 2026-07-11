package api

import (
	"net/http"
	"os"

	"golang.org/x/crypto/bcrypt"
)

func (s *Server) handleAuth(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Password string `json:"password"`
	}
	if err := decodeJSON(w, r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request")
		return
	}

	hash := os.Getenv("CLIENT_PASSWORD_HASH")
	if hash == "" {
		writeError(w, http.StatusNotFound, "password auth not configured")
		return
	}

	if bcrypt.CompareHashAndPassword([]byte(hash), []byte(body.Password)) != nil {
		writeError(w, http.StatusUnauthorized, "invalid password")
		return
	}

	if s.apiToken == "" {
		writeError(w, http.StatusInternalServerError, "server token not configured")
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{
		"token": s.apiToken,
	})
}
