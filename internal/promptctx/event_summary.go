package promptctx

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// eventGroup groups events by reason and involved object for dedup.
type eventGroup struct {
	reason   string
	involved string
}

// SummarizeEvents deduplicates K8s events by reason + involved object,
// returning compact summaries sorted by count (descending).
// Each summary includes the event count and relative last-seen time.
func SummarizeEvents(events []K8sEvent, now time.Time) []EventSummary {
	if len(events) == 0 {
		return nil
	}

	type groupInfo struct {
		count    int
		lastSeen time.Time
	}
	grouped := make(map[eventGroup]*groupInfo)

	for _, e := range events {
		g := eventGroup{reason: e.Reason, involved: e.Involved}
		info, ok := grouped[g]
		if !ok {
			info = &groupInfo{}
			grouped[g] = info
		}
		info.count++
		if e.LastTimestamp.After(info.lastSeen) {
			info.lastSeen = e.LastTimestamp
		}
	}

	result := make([]EventSummary, 0, len(grouped))
	for g, info := range grouped {
		result = append(result, EventSummary{
			Reason:   g.reason,
			Count:    info.count,
			LastSeen: relativeTime(info.lastSeen, now),
			Involved: g.involved,
		})
	}

	sort.Slice(result, func(i, j int) bool {
		return result[i].Count > result[j].Count
	})

	return result
}

// K8sEvent is a lightweight representation of a Kubernetes event.
type K8sEvent struct {
	Reason        string
	Involved      string    // namespace/name
	LastTimestamp time.Time
}

// relativeTime returns a human-readable relative time like "2m ago" or "1h ago".
func relativeTime(t, now time.Time) string {
	if t.IsZero() {
		return ""
	}
	d := now.Sub(t)
	if d < 0 {
		d = 0
	}
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds ago", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd ago", int(d.Hours()/24))
	}
}

// shortEventMessage truncates a long event message for token efficiency.
func shortEventMessage(msg string, maxLen int) string {
	msg = strings.ReplaceAll(msg, "\n", " ")
	if len(msg) <= maxLen {
		return msg
	}
	return msg[:maxLen] + "..."
}
