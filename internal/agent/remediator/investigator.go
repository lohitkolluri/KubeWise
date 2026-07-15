package remediator

import (
	"context"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/lohitkolluri/KubeWise/internal/promptctx"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

const (
	// Token efficiency defaults: keep investigation concise.
	maxLogBytesPerContainer = 3072
	maxLogTailLines         = int64(80)
	maxEvents               = 12
	maxSuccessorPods        = 3
	perAPICallTimeout       = 5 * time.Second

	// Hard cap across ALL targets for prompt size control.
	maxInvestigationChars = 12000
)

// Investigator gathers live cluster context (describe, logs, events) for RCA.
type Investigator struct {
	clientset kubernetes.Interface
	lokiURL   string
	tempoURL  string
}

// NewInvestigator creates an investigator using in-cluster config.
func NewInvestigator(clientset kubernetes.Interface) *Investigator {
	return &Investigator{clientset: clientset}
}

// NewInvestigatorWithObservability enables Loki/Tempo lookups for prompt context enrichment.
func NewInvestigatorWithObservability(clientset kubernetes.Interface, lokiURL, tempoURL string) *Investigator {
	return &Investigator{
		clientset: clientset,
		lokiURL:   strings.TrimSpace(lokiURL),
		tempoURL:  strings.TrimSpace(tempoURL),
	}
}

// SetObservabilityURLs updates Loki/Tempo endpoints at runtime (e.g. after config PUT).
func (inv *Investigator) SetObservabilityURLs(lokiURL, tempoURL string) {
	if inv == nil {
		return
	}
	inv.lokiURL = strings.TrimSpace(lokiURL)
	inv.tempoURL = strings.TrimSpace(tempoURL)
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

	apiCtx, cancel := inv.apiCallCtx(ctx)
	pod, err := inv.clientset.CoreV1().Pods(namespace).Get(apiCtx, name, metav1.GetOptions{})
	cancel()
	if err != nil {
		apiCtx, cancel := inv.apiCallCtx(ctx)
		node, nerr := inv.clientset.CoreV1().Nodes().Get(apiCtx, name, metav1.GetOptions{})
		cancel()
		if nerr == nil {
			inv.writeNodeSummary(b, node)
			return
		}
		inv.writePodMissingSummary(b, err)
		inv.writeEventsSection(b, inv.listPodEvents(ctx, namespace, name))
		inv.writeObservabilitySection(ctx, b, namespace, name)
		inv.writeWorkloadSuccessor(ctx, b, namespace, name)
		b.WriteString("\n")
		return
	}

	inv.writePodDescribe(b, pod)
	inv.writeEventsSection(b, inv.listPodEvents(ctx, namespace, name))
	for _, line := range inv.podLogs(ctx, namespace, name, pod) {
		b.WriteString(line)
	}
	inv.writeObservabilitySection(ctx, b, namespace, name)
	b.WriteString("\n")
}

func (inv *Investigator) writePodDescribe(b *strings.Builder, pod *corev1.Pod) {
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
}

func (inv *Investigator) writePodMissingSummary(b *strings.Builder, err error) {
	switch {
	case apierrors.IsNotFound(err):
		b.WriteString("- describe: pod not found (likely deleted or replaced by a rollout)\n")
	case apierrors.IsForbidden(err):
		b.WriteString("- describe: forbidden (check agent RBAC for pods/get)\n")
	default:
		fmt.Fprintf(b, "- describe: unavailable (%v)\n", err)
	}
}

func (inv *Investigator) writeEventsSection(b *strings.Builder, events []corev1.Event) {
	if len(events) == 0 {
		b.WriteString("- events: none found for this object\n")
		return
	}
	sort.Slice(events, func(i, j int) bool {
		return events[i].LastTimestamp.After(events[j].LastTimestamp.Time)
	})
	b.WriteString("- recent events:\n")
	limit := len(events)
	if limit > maxEvents {
		limit = maxEvents
	}
	for _, e := range events[:limit] {
		fmt.Fprintf(b, "  - [%s] %s: %s\n", e.Reason, e.Type, truncate(e.Message, 200))
	}
}

func (inv *Investigator) writeObservabilitySection(ctx context.Context, b *strings.Builder, namespace, name string) {
	if inv.lokiURL != "" {
		snippets, err := promptctx.FetchLogSnippets(ctx, inv.lokiURL, namespace, name, 15*time.Minute)
		switch {
		case err != nil:
			fmt.Fprintf(b, "- loki: unavailable (%v)\n", err)
		case len(snippets) > 0:
			b.WriteString("- loki (recent error-ish lines):\n")
			for i, s := range snippets {
				if i >= 12 {
					break
				}
				fmt.Fprintf(b, "  - [%s] %s %s\n", s.Container, s.Timestamp, truncate(s.Line, 200))
			}
		default:
			b.WriteString("- loki: no matching error lines in last 15m (pod may be gone or logs not ingested)\n")
		}
	}
	if inv.tempoURL != "" {
		traces, err := promptctx.FetchTraceContext(ctx, inv.tempoURL, namespace, name, 15*time.Minute)
		switch {
		case err != nil:
			fmt.Fprintf(b, "- tempo: unavailable (%v)\n", err)
		case len(traces) > 0:
			b.WriteString("- tempo (recent traces):\n")
			for i, t := range traces {
				if i >= 8 {
					break
				}
				fmt.Fprintf(b, "  - trace=%s dur=%dms spans=%d services=%d start=%s root=%s\n",
					t.TraceID, t.DurationMs, t.SpanCount, t.Services, t.StartTime, truncate(t.RootName, 60))
			}
		default:
			b.WriteString("- tempo: no traces in last 15m\n")
		}
	}
}

// writeWorkloadSuccessor helps when the target pod name is ephemeral (Deployment rollouts).
func (inv *Investigator) writeWorkloadSuccessor(ctx context.Context, b *strings.Builder, namespace, podName string) {
	depName := inferDeploymentFromPod(podName)
	if depName == "" {
		return
	}
	apiCtx, cancel := inv.apiCallCtx(ctx)
	dep, err := inv.clientset.AppsV1().Deployments(namespace).Get(apiCtx, depName, metav1.GetOptions{})
	cancel()
	if err != nil {
		if apierrors.IsNotFound(err) {
			return
		}
		fmt.Fprintf(b, "- successor workload: deployment/%s lookup failed (%v)\n", depName, err)
		return
	}

	avail := int32(0)
	if dep.Status.AvailableReplicas > 0 {
		avail = dep.Status.AvailableReplicas
	}
	fmt.Fprintf(b, "- successor workload: deployment/%s replicas=%d/%d available=%d\n",
		depName, dep.Status.ReadyReplicas, dep.Status.Replicas, avail)

	selector := metav1.FormatLabelSelector(dep.Spec.Selector)
	if selector == "" {
		return
	}
	listCtx, listCancel := inv.apiCallCtx(ctx)
	pods, err := inv.clientset.CoreV1().Pods(namespace).List(listCtx, metav1.ListOptions{
		LabelSelector: selector,
		Limit:         maxSuccessorPods,
	})
	listCancel()
	if err != nil {
		fmt.Fprintf(b, "- successor pods: list failed (%v)\n", err)
		return
	}
	if len(pods.Items) == 0 {
		b.WriteString("- successor pods: none currently scheduled\n")
		return
	}

	b.WriteString("- successor pods:\n")
	for _, p := range pods.Items {
		if p.Name == podName {
			continue
		}
		ready := podReadyCount(&p)
		fmt.Fprintf(b, "  - %s phase=%s ready=%d/%d\n",
			p.Name, p.Status.Phase, ready, len(p.Spec.Containers))
	}
}

// inferDeploymentFromPod extracts a Deployment name from a standard pod name.
// Delegates to inferDeploymentFromPodName for consistent handling across the codebase.
func inferDeploymentFromPod(podName string) string {
	return inferDeploymentFromPodName(podName)
}

func podReadyCount(pod *corev1.Pod) int {
	if pod == nil {
		return 0
	}
	ready := 0
	for _, cs := range pod.Status.ContainerStatuses {
		if cs.Ready {
			ready++
		}
	}
	return ready
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
	apiCtx, cancel := inv.apiCallCtx(ctx)
	defer cancel()
	list, err := inv.clientset.CoreV1().Events(namespace).List(apiCtx, metav1.ListOptions{
		FieldSelector: field,
		Limit:         maxEvents * 2,
	})
	if err != nil {
		return nil
	}
	return list.Items
}

func (inv *Investigator) podLogs(ctx context.Context, namespace, name string, pod *corev1.Pod) []string {
	if pod == nil {
		return []string{"- logs: unavailable (pod object missing)\n"}
	}
	var lines []string
	for _, c := range pod.Spec.Containers {
		apiCtx, cancel := inv.apiCallCtx(ctx)
		req := inv.clientset.CoreV1().Pods(namespace).GetLogs(name, &corev1.PodLogOptions{
			Container: c.Name,
			TailLines: int64Ptr(maxLogTailLines),
		})
		stream, err := req.Stream(apiCtx)
		cancel()
		if err != nil {
			lines = append(lines, fmt.Sprintf("- logs %s: unavailable (%v)\n", c.Name, friendlyLogError(err)))
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

func friendlyLogError(err error) error {
	if err == nil {
		return nil
	}
	if apierrors.IsNotFound(err) {
		return fmt.Errorf("pod/container gone (logs only exist while the pod object exists)")
	}
	if apierrors.IsForbidden(err) {
		return fmt.Errorf("forbidden (check agent RBAC for pods/log)")
	}
	return err
}

// compactLog reduces token usage while keeping error signal.
func compactLog(text string, keepLast int) string {
	raw := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	raw = strings.Split(strings.TrimSpace(strings.Join(raw, "\n")), "\n")

	stripTS := func(s string) string {
		s = strings.TrimSpace(s)
		if len(s) > 20 && s[4] == '-' && s[7] == '-' {
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

	errish := make(map[string]struct{})
	for i := maxint(0, len(cleaned)-200); i < len(cleaned); i++ {
		l := strings.ToLower(cleaned[i])
		if strings.Contains(l, "error") || strings.Contains(l, "fail") || strings.Contains(l, "panic") ||
			strings.Contains(l, "exception") || strings.Contains(l, "timeout") || strings.Contains(l, "crash") {
			errish[cleaned[i]] = struct{}{}
		}
	}

	start := maxint(0, len(cleaned)-keepLast)
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

func maxint(a, b int) int {
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

func (inv *Investigator) apiCallCtx(parent context.Context) (context.Context, context.CancelFunc) {
	if deadline, ok := parent.Deadline(); ok {
		remaining := time.Until(deadline)
		if remaining < perAPICallTimeout {
			return context.WithTimeout(parent, remaining)
		}
	}
	return context.WithTimeout(parent, perAPICallTimeout)
}
