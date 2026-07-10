package cli

import (
	"strings"
	"time"

	"charm.land/lipgloss/v2"
)

type toastKind int

const (
	toastInfo toastKind = iota
	toastSuccess
	toastWarn
	toastError
)

type toastItem struct {
	text  string
	kind  toastKind
	until time.Time
}

type toastStack struct {
	items []toastItem
}

const toastMaxItems = 4

func (t *toastStack) push(text string, kind toastKind, d time.Duration) {
	if strings.TrimSpace(text) == "" {
		return
	}
	t.items = append(t.items, toastItem{text: text, kind: kind, until: time.Now().Add(d)})
	if len(t.items) > toastMaxItems {
		t.items = t.items[len(t.items)-toastMaxItems:]
	}
}

func (t *toastStack) prune(now time.Time) {
	if len(t.items) == 0 {
		return
	}
	kept := t.items[:0]
	for _, item := range t.items {
		if now.Before(item.until) {
			kept = append(kept, item)
		}
	}
	t.items = kept
}

func toastLineStyle(kind toastKind) lipgloss.Style {
	switch kind {
	case toastSuccess:
		return successStyle.Padding(0, 1)
	case toastWarn:
		return warnStyle.Padding(0, 1)
	case toastError:
		return errStyle.Padding(0, 1)
	default:
		return infoStyle.Padding(0, 1)
	}
}

func toastPrefix(kind toastKind) string {
	switch kind {
	case toastSuccess:
		return "✓ "
	case toastWarn:
		return "⚠ "
	case toastError:
		return "✗ "
	default:
		return "· "
	}
}

func (t toastStack) strip() string {
	if len(t.items) == 0 {
		return ""
	}
	start := 0
	if len(t.items) > 2 {
		start = len(t.items) - 2
	}
	lines := make([]string, 0, len(t.items)-start)
	for _, item := range t.items[start:] {
		lines = append(lines, toastLineStyle(item.kind).Render(toastPrefix(item.kind)+item.text))
	}
	return strings.Join(lines, "\n")
}

func (m *controlModel) notify(text string, kind toastKind) {
	m.toasts.push(text, kind, 3*time.Second)
}

func (m *controlModel) notifyErr(text string) {
	m.toasts.push(text, toastError, 5*time.Second)
}
