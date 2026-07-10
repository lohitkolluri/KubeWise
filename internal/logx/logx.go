// Package logx configures structured logging for KubeWise binaries.
package logx

import (
	"io"
	"log/slog"
	"os"
	"strings"

	charmlog "github.com/charmbracelet/log"
)

// Setup installs a charmbracelet/log slog handler as the default logger.
func Setup(w io.Writer) {
	if w == nil {
		w = os.Stderr
	}
	logger := charmlog.NewWithOptions(w, charmlog.Options{
		ReportTimestamp: true,
		TimeFormat:      "15:04:05",
	})
	logger.SetLevel(parseLevel(os.Getenv("KUBEWISE_LOG_LEVEL")))
	if lvl := os.Getenv("LOG_LEVEL"); os.Getenv("KUBEWISE_LOG_LEVEL") == "" && lvl != "" {
		logger.SetLevel(parseLevel(lvl))
	}
	slog.SetDefault(slog.New(logger))
}

func parseLevel(raw string) charmlog.Level {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "debug", "trace":
		return charmlog.DebugLevel
	case "warn", "warning":
		return charmlog.WarnLevel
	case "error":
		return charmlog.ErrorLevel
	case "fatal":
		return charmlog.FatalLevel
	default:
		return charmlog.InfoLevel
	}
}
