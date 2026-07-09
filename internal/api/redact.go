package api

import (
	"regexp"
	"strings"
)

var bearerTokenRE = regexp.MustCompile(`(?i)Bearer\s+\S+`)

func redactSensitive(s string) string {
	if s == "" {
		return s
	}
	s = bearerTokenRE.ReplaceAllString(s, "Bearer ***")
	if i := strings.Index(s, "sk-"); i >= 0 {
		j := i
		for j < len(s) && s[j] != ' ' && s[j] != '\n' && s[j] != '\t' && s[j] != '"' && s[j] != '\'' {
			j++
		}
		s = s[:i] + "sk-***" + s[j:]
	}
	return s
}
