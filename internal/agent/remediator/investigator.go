package remediator

import (
	"context"
	"fmt"
	"io"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const (
	// Token efficiency defaults: keep investigation concise.
	// We compact logs further below (dedupe + strip timestamps), so we can keep
	// byte/line limits modest without losing key error signals.
	maxLogBytesPerContainer = 3072
	maxLogTailLines         = int64(80)
	maxEvents               = 12

	// Hard cap across ALL targets for prompt size control.
	maxInvestigationChars = 12000
)

// Investigator gathers live cluster context (describe, logs, events) for RCA.
type Investigator struct {
	clientset kubernetes.Interface
}

// NewInvestigator creates an investigator using in-cluster config.
func NewInvestigator(clientset kubernetes.Interface) *Investigator {
	return &Investigator{clientset: clientset}
}

// Gather collects investigation context for the given anomalies.
func (inv *Investigator) Gather(ctx context.Context, anomalies []models.AnomalyRecord) string {
	if inv == nil || inv.clientset == nil || len(anomalies) == 0 {
		return ""
	}

	type target struct {
		namespace string
		name      string
	}
	seen := make(map[string]target)
	var order []target
	for _, a := range anomalies {
		ns, name := models.ParseEntity(a.Entity)
		if a.Namespace != "" && ns == "" {
			ns = a.Namespace
		}
		if ns == "" || name == "" {
			continue
		}
		key := ns + "/" + name
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = target{namespace: ns, name: name}
		order = append(order, target{namespace: ns, name: name})
	}

	var b strings.Builder
	for _, t := range order {
		inv.writePodInvestigation(ctx, &b, t.namespace, t.name)
		if b.Len() >= maxInvestigationChars {
			b.WriteString("\n[truncated: investigation context capped]\n")
			break
		}
	}
	return strings.TrimSpace(b.String())
}

func (inv *Investigator) writePodInvestigation(ctx context.Context, b *strings.Builder, namespace, name string) {
	fmt.Fprintf(b, "WORKLOAD %s/%s\n", namespace, name)

	pod, err := inv.clientset.CoreV1().Pods(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		if node, nerr := inv.clientset.CoreV1().Nodes().Get(ctx, name, metav1.GetOptions{}); nerr == nil {
			inv.writeNodeSummary(b, node)
			return
		}
		fmt.Fprintf(b, "- describe: unavailable (%v)\n", err)
		return
	}
	fmt.Fprintf(b, "- phase: %s\n", pod.Status.Phase)
	for _, cond := range pod.Status.Conditions {
		if cond.Status != corev1.ConditionTrue {
			continue
		}
		fmt.Fprintf(b, "- condition: %s=%s (%s)\n", cond.Type, cond.Status, cond.Reason)
	}
	for _, cs := range pod.Status.ContainerStatuses {
		state := containerStateSummary(cs)
		fmt.Fprintf(b, "- container %s: ready=%v restarts=%d %s\n",
			cs.Name, cs.Ready, cs.RestartCount, state)
	}

	events := inv.listPodEvents(ctx, namespace, name)
	if len(events) > 0 {
		b.WriteString("- recent events:\n")
		for _, e := range events {
			fmt.Fprintf(b, "  - [%s] %s: %s\n", e.Reason, e.Type, truncate(e.Message, 200))
		}
	}

	for _, line := range inv.podLogs(ctx, namespace, name, pod) {
		b.WriteString(line)
	}
	b.WriteString("\n")
}

func (inv *Investigator) writeNodeSummary(b *strings.Builder, node *corev1.Node) {
	fmt.Fprintf(b, "NODE %s\n", node.Name)
	for _, cond := range node.Status.Conditions {
		if cond.Status != corev1.ConditionTrue {
			continue
		}
		fmt.Fprintf(b, "- condition: %s=%s (%s)\n", cond.Type, cond.Status, cond.Reason)
	}
	b.WriteString("\n")
}

func (inv *Investigator) listPodEvents(ctx context.Context, namespace, name string) []corev1.Event {
	field := fmt.Sprintf("involvedObject.name=%s,involvedObject.namespace=%s", name, namespace)
	list, err := inv.clientset.CoreV1().Events(namespace).List(ctx, metav1.ListOptions{
		FieldSelector: field,
		Limit:         maxEvents,
	})
	if err != nil {
		return nil
	}
	return list.Items
}

func (inv *Investigator) podLogs(ctx context.Context, namespace, name string, pod *corev1.Pod) []string {
	if pod == nil {
		return nil
	}
	var lines []string
	for _, c := range pod.Spec.Containers {
		req := inv.clientset.CoreV1().Pods(namespace).GetLogs(name, &corev1.PodLogOptions{
			Container: c.Name,
			TailLines: int64Ptr(maxLogTailLines),
		})
		stream, err := req.Stream(ctx)
		if err != nil {
			lines = append(lines, fmt.Sprintf("- logs %s: unavailable (%v)\n", c.Name, err))
			continue
		}
		data, err := io.ReadAll(io.LimitReader(stream, maxLogBytesPerContainer))
		_ = stream.Close()
		if err != nil {
			lines = append(lines, fmt.Sprintf("- logs %s: read error (%v)\n", c.Name, err))
			continue
		}
		text := strings.TrimSpace(string(data))
		if text == "" {
			lines = append(lines, fmt.Sprintf("- logs %s: (empty)\n", c.Name))
			continue
		}
		lines = append(lines, fmt.Sprintf("- logs %s (tail, compacted):\n%s\n", c.Name, compactLog(text, 48)))
	}
	return lines
}

// compactLog reduces token usage while keeping error signal:
// - strips obvious RFC3339-like timestamp prefixes
// - collapses consecutive duplicate lines
// - keeps the last N lines, plus any recent "error-ish" lines
func compactLog(text string, keepLast int) string {
	raw := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	raw = strings.Split(strings.TrimSpace(strings.Join(raw, "\n")), "\n")

	stripTS := func(s string) string {
		s = strings.TrimSpace(s)
		// Heuristic timestamp strip: "2026-07-07T07:21:13Z ..." or "2026-07-07 07:21:13 ..."
		if len(s) > 20 && s[4] == '-' && s[7] == '-' {
			// Find first space after date/time chunk.
			if idx := strings.IndexByte(s, ' '); idx > 0 && idx < 40 {
				return strings.TrimSpace(s[idx+1:])
			}
		}
		return s
	}

	var cleaned []string
	prev := ""
	for _, ln := range raw {
		ln = stripTS(ln)
		if ln == "" {
			continue
		}
		if ln == prev {
			continue
		}
		prev = ln
		cleaned = append(cleaned, ln)
	}
	if len(cleaned) == 0 {
		return "(empty)"
	}

	// Collect "error-ish" lines from the tail window.
	errish := make(map[string]struct{})
	for i := max(0, len(cleaned)-200); i < len(cleaned); i++ {
		l := strings.ToLower(cleaned[i])
		if strings.Contains(l, "error") || strings.Contains(l, "fail") || strings.Contains(l, "panic") ||
			strings.Contains(l, "exception") || strings.Contains(l, "timeout") || strings.Contains(l, "crash") {
			errish[cleaned[i]] = struct{}{}
		}
	}

	start := max(0, len(cleaned)-keepLast)
	var out []string
	seen := make(map[string]struct{})
	for i := start; i < len(cleaned); i++ {
		ln := cleaned[i]
		if _, ok := seen[ln]; ok {
			continue
		}
		seen[ln] = struct{}{}
		out = append(out, ln)
	}
	// Append err-ish lines not already included (bounded).
	added := 0
	for ln := range errish {
		if _, ok := seen[ln]; ok {
			continue
		}
		out = append(out, ln)
		added++
		if added >= 8 {
			break
		}
	}

	return strings.Join(out, "\n")
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func containerStateSummary(cs corev1.ContainerStatus) string {
	if cs.State.Waiting != nil {
		return fmt.Sprintf("waiting reason=%s msg=%s", cs.State.Waiting.Reason, truncate(cs.State.Waiting.Message, 120))
	}
	if cs.State.Terminated != nil {
		return fmt.Sprintf("terminated reason=%s exit=%d", cs.State.Terminated.Reason, cs.State.Terminated.ExitCode)
	}
	if cs.State.Running != nil {
		return "running"
	}
	return "unknown"
}

func truncate(s string, n int) string {
	s = strings.ReplaceAll(s, "\n", " ")
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func int64Ptr(v int64) *int64 { return &v }

// gatherTimeout bounds investigation API calls.
func gatherTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	if deadline, ok := parent.Deadline(); ok {
		remaining := time.Until(deadline)
		if remaining < 45*time.Second {
			return context.WithTimeout(parent, remaining)
		}
	}
	return context.WithTimeout(parent, 45*time.Second)
}
