package cli

import (
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
)

func colorizeLogs(text string) string {
	if text == "" {
		return ""
	}
	text = ansi.Strip(text)
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		lines[i] = colorizeLogLine(line)
	}
	return strings.Join(lines, "\n")
}

func colorizeLogLine(line string) string {
	if strings.TrimSpace(line) == "" {
		return line
	}
	level, ok := detectLogLevel(line)
	if !ok {
		return subtleStyle.Render(line)
	}
	return logLevelStyle(level).Render(line)
}

type logLevel int

const (
	logLevelDebug logLevel = iota
	logLevelInfo
	logLevelWarn
	logLevelError
)

func detectLogLevel(line string) (logLevel, bool) {
	lower := strings.ToLower(line)
	switch {
	case hasLogToken(lower, "fatal") || hasLogToken(lower, "error") || strings.Contains(lower, "\"severity\":\"error\""):
		return logLevelError, true
	case hasLogToken(lower, "warn"), strings.Contains(lower, "\"severity\":\"warn\""):
		return logLevelWarn, true
	case hasLogToken(lower, "info"), strings.Contains(lower, "\"severity\":\"info\""):
		return logLevelInfo, true
	case hasLogToken(lower, "debug"), hasLogToken(lower, "trace"), hasLogToken(lower, "dbug"):
		return logLevelDebug, true
	default:
		return 0, false
	}
}

func hasLogToken(line, token string) bool {
	if strings.HasPrefix(line, token) {
		return true
	}
	return strings.Contains(line, " "+token+" ") ||
		strings.Contains(line, "level="+token) ||
		strings.Contains(line, "\"level\":\""+token)
}

// logLevelStyle mirrors charmbracelet/log level colors using the KubeWise theme.
func logLevelStyle(level logLevel) lipgloss.Style {
	switch level {
	case logLevelError:
		return errStyle
	case logLevelWarn:
		return warnStyle
	case logLevelInfo:
		return infoStyle
	default:
		return mutedStyle
	}
}
