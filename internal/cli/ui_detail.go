package cli

import (
	"strings"
	"sync"

	"github.com/charmbracelet/glamour"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

const glamourGutter = 3

var (
	glamourRenderer     *glamour.TermRenderer
	glamourRendererW    int
	glamourRendererMu   sync.Mutex
	glamourRendererDark bool
)

// detailContentWidth returns the inner width available for detail text inside the viewport.
func (m controlModel) detailContentWidth() int {
	w := m.detailVP.Width
	if w <= 0 {
		w = 80
	}
	frame := m.detailVP.Style.GetHorizontalFrameSize()
	inner := w - frame
	if inner < 20 {
		return 20
	}
	return inner
}

func writeDetailKV(b *strings.Builder, width int, label, value string) {
	b.WriteString(formatDetailKV(width, label, value, detailValueStyle))
	b.WriteByte('\n')
}

func writeDetailKVStyled(b *strings.Builder, width int, label string, valStyle lipgloss.Style, value string) {
	b.WriteString(formatDetailKV(width, label, value, valStyle))
	b.WriteByte('\n')
}

func formatDetailKV(width int, label, value string, valStyle lipgloss.Style) string {
	return formatDetailKVRendered(width, label, valStyle.Render(value))
}

func formatDetailKVRendered(width int, label, renderedValue string) string {
	labelR := detailLabelStyle.Render(label)
	labelW := lipgloss.Width(labelR)
	gutter := strings.Repeat(" ", labelW+1)
	avail := width - labelW - 1
	if avail < 8 {
		avail = 8
	}

	if label == "" {
		return gutter + renderedValue
	}

	wrapped := ansi.Wrap(renderedValue, avail, "")
	lines := strings.Split(wrapped, "\n")
	out := make([]string, 0, len(lines))
	for i, line := range lines {
		if i == 0 {
			out = append(out, lipgloss.JoinHorizontal(lipgloss.Top, labelR, line))
			continue
		}
		out = append(out, gutter+line)
	}
	return strings.Join(out, "\n")
}

func writeDetailSection(b *strings.Builder, title string) {
	if b.Len() > 0 {
		b.WriteByte('\n')
	}
	b.WriteString(detailSectionStyle.Render("── " + strings.TrimSpace(title) + " ──"))
	b.WriteByte('\n')
}

func writeDetailProse(b *strings.Builder, width int, text string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	rendered := renderMarkdownBody(text, width)
	rendered = strings.TrimRight(rendered, "\n")
	if rendered == "" {
		return
	}
	b.WriteString(rendered)
	b.WriteByte('\n')
}

func writeDetailSectionProse(b *strings.Builder, width int, title, text string) {
	writeDetailSection(b, title)
	writeDetailProse(b, width, text)
}

func renderMarkdownBody(text string, width int) string {
	text = strings.TrimSpace(text)
	if text == "" {
		return ""
	}
	if width < 20 {
		width = 20
	}
	mdWidth := width - glamourGutter
	if mdWidth < 16 {
		mdWidth = 16
	}

	dark := lipgloss.HasDarkBackground()
	r, err := glamourRendererFor(mdWidth, dark)
	if err != nil {
		return mutedStyle.Render(ansi.Wrap(text, width, " "))
	}
	out, err := r.Render(text)
	if err != nil {
		return mutedStyle.Render(ansi.Wrap(text, width, " "))
	}
	return strings.TrimRight(out, "\n")
}

func glamourRendererFor(width int, dark bool) (*glamour.TermRenderer, error) {
	glamourRendererMu.Lock()
	defer glamourRendererMu.Unlock()

	if glamourRenderer != nil && glamourRendererW == width && glamourRendererDark == dark {
		return glamourRenderer, nil
	}

	style := "light"
	if dark {
		style = "dark"
	}
	r, err := glamour.NewTermRenderer(
		glamour.WithStandardStyle(style),
		glamour.WithWordWrap(width),
	)
	if err != nil {
		return nil, err
	}
	glamourRenderer = r
	glamourRendererW = width
	glamourRendererDark = dark
	return r, nil
}

func detailMetaLines(meta string) int {
	meta = strings.TrimRight(meta, "\n")
	if meta == "" {
		return 0
	}
	return lipgloss.Height(meta)
}
