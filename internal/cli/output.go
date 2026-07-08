package cli

import (
	"encoding/json"
	"fmt"
	"io"
	"unicode/utf8"

	yaml "gopkg.in/yaml.v3"
)

func validateOutputFormat() error {
	switch outputFormat {
	case "table", "json", "yaml":
		return nil
	default:
		return fmt.Errorf("invalid output format %q: use table, json, or yaml", outputFormat)
	}
}

func writeOutput(w io.Writer, format string, v any, tableFn func() error) error {
	switch format {
	case "json":
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		return enc.Encode(v)
	case "yaml":
		enc := yaml.NewEncoder(w)
		enc.SetIndent(2)
		defer func() { _ = enc.Close() }()
		return enc.Encode(v)
	default:
		return tableFn()
	}
}

func trunc(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	// Truncate by runes, not bytes, to avoid splitting multi-byte characters
	var runes int
	for i := range s {
		if runes >= n-1 {
			return s[:i] + "…"
		}
		runes++
	}
	return s
}

func repeatLine(n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = '-'
	}
	return string(b)
}
