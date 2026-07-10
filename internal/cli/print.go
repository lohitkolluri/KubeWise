package cli

import (
	"fmt"
	"io"
	"os"
	"strings"

	"charm.land/lipgloss/v2"
	"charm.land/lipgloss/v2/table"
)

func writerTTY(w io.Writer) bool {
	if f, ok := w.(*os.File); ok {
		return isTerminal(f)
	}
	return false
}

func printBanner(w io.Writer) {
	if !writerTTY(w) {
		return
	}
	_, _ = fmt.Fprintln(w, logoStyle.Render(" KubeWise ")+brandStyle.Render(" kwctl"))
}

func printSection(w io.Writer, title string) {
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, headingStyle.Render(title))
		return
	}
	_, _ = fmt.Fprintln(w, title)
}

func printKV(w io.Writer, label, value string) {
	printKVStyled(w, label, value, detailValueStyle)
}

func printKVStyled(w io.Writer, label, value string, valStyle lipgloss.Style) {
	if writerTTY(w) {
		_, _ = fmt.Fprintf(w, "%s %s\n", detailLabelStyle.Render(label), valStyle.Render(value))
		return
	}
	_, _ = fmt.Fprintf(w, "%-22s %s\n", label, value)
}

func printOK(w io.Writer, format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, successStyle.Render("✓ "+msg))
		return
	}
	_, _ = fmt.Fprintln(w, "OK "+msg)
}

func printFail(w io.Writer, format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, errStyle.Render("✗ "+msg))
		return
	}
	_, _ = fmt.Fprintln(w, "FAIL "+msg)
}

func printWarn(w io.Writer, format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, warnStyle.Render("⚠ "+msg))
		return
	}
	_, _ = fmt.Fprintln(w, "WARN "+msg)
}

func printOptional(w io.Writer, format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, mutedStyle.Render("○ "+msg))
		return
	}
	_, _ = fmt.Fprintln(w, "○ "+msg)
}

func printHint(w io.Writer, format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, mutedStyle.Render("  → "+msg))
		return
	}
	_, _ = fmt.Fprintln(w, "  -> "+msg)
}

func printEmpty(w io.Writer, msg string) {
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, emptyStateStyle.Render(msg))
		return
	}
	_, _ = fmt.Fprintln(w, msg)
}

func printNextSteps(w io.Writer, steps ...string) {
	if len(steps) == 0 {
		return
	}
	if writerTTY(w) {
		_, _ = fmt.Fprintln(w, "")
		_, _ = fmt.Fprintln(w, headingStyle.Render("Next"))
	} else {
		_, _ = fmt.Fprintln(w, "\nNext:")
	}
	for _, step := range steps {
		if writerTTY(w) {
			_, _ = fmt.Fprintln(w, keyStyle.Render("  "+step))
		} else {
			_, _ = fmt.Fprintln(w, "  "+step)
		}
	}
}

func printDataTable(w io.Writer, headers []string, rows [][]string) {
	if len(headers) == 0 {
		return
	}
	if !writerTTY(w) {
		printPlainTable(w, headers, rows)
		return
	}
	_, _ = fmt.Fprintln(w, renderStyledTable(headers, rows))
}

func renderStyledTable(headers []string, rows [][]string) string {
	t := table.New().
		Headers(headers...).
		Border(lipgloss.NormalBorder()).
		BorderHeader(true).
		BorderTop(false).
		BorderBottom(true).
		BorderLeft(false).
		BorderRight(false).
		StyleFunc(func(row, _ int) lipgloss.Style {
			if row == table.HeaderRow {
				return lipgloss.NewStyle().Bold(true).Foreground(colorMuted)
			}
			return lipgloss.NewStyle().Foreground(colorSubtle)
		})
	for _, row := range rows {
		t = t.Row(row...)
	}
	return t.String()
}

func printPlainTable(w io.Writer, headers []string, rows [][]string) {
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = len(h)
	}
	for _, row := range rows {
		for i, cell := range row {
			if i < len(widths) && len(cell) > widths[i] {
				widths[i] = len(cell)
			}
		}
	}
	writeRow := func(cells []string) {
		for i, cell := range cells {
			if i > 0 {
				_, _ = fmt.Fprint(w, "  ")
			}
			if i < len(widths) {
				_, _ = fmt.Fprintf(w, "%-*s", widths[i], cell)
			} else {
				_, _ = fmt.Fprint(w, cell)
			}
		}
		_, _ = fmt.Fprintln(w)
	}
	writeRow(headers)
	_, _ = fmt.Fprintln(w, strings.Repeat("-", 72))
	for _, row := range rows {
		writeRow(row)
	}
}

func formatCLIError(err error) string {
	if err == nil {
		return ""
	}
	if isTerminal(os.Stderr) {
		return errStyle.Render("error: ") + subtleStyle.Render(err.Error())
	}
	return "error: " + err.Error()
}
