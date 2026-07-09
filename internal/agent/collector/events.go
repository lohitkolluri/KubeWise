package collector

import (
	"context"
	"log/slog"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"

	nsutil "github.com/lohitkolluri/KubeWise/pkg/namespace"
)

// failureReasons lists K8s event reasons that indicate potential failures.
var failureReasons = map[string]bool{
	"BackOff":            true,
	"OOMKilling":         true,
	"FailedMount":        true,
	"ImagePull":          true,
	"ImagePullBackOff":   true,
	"CrashLoopBackOff":   true,
	"ProbeError":         true,
	"NodeCondition":      true,
	"NodeNotReady":       true,
	"Evicted":            true,
	"FailedPlacement":    true,
	"FailedScheduling":   true,
	"FailedNodeAffinity": true,
	"OutOfDisk":          true,
	"MemoryPressure":     true,
	"DiskPressure":       true,
}

// EventRecord represents a filtered failure-related K8s event.
type EventRecord struct {
	Type           string    `json:"type"`
	Reason         string    `json:"reason"`
	Message        string    `json:"message"`
	Count          int32     `json:"count"`
	InvolvedObject string    `json:"involved_object"`
	Namespace      string    `json:"namespace"`
	Source         string    `json:"source"`
	FirstTimestamp time.Time `json:"first_timestamp"`
	LastTimestamp  time.Time `json:"last_timestamp"`
}

// EventsCollector watches K8s events using a SharedInformer and emits failure-related records.
// The informer handles reconnection automatically with bookmark events,
// eliminating the "resourceVersion too old" errors that the raw Watch() loop produced.
type EventsCollector struct {
	clientset       kubernetes.Interface
	namespace       string
	watchNamespaces []string
	informer        cache.Controller
	synced          bool
}

// NewEventsCollector creates a new events collector.
func NewEventsCollector(clientset kubernetes.Interface, namespace string, watchNamespaces []string) *EventsCollector {
	return &EventsCollector{
		clientset:       clientset,
		namespace:       namespace,
		watchNamespaces: watchNamespaces,
	}
}

// WatchEvents starts watching events using a SharedInformer and returns a channel of EventRecords.
// The informer handles reconnection with bookmark events (no manual backoff needed).
func (ec *EventsCollector) WatchEvents(ctx context.Context) <-chan EventRecord {
	ch := make(chan EventRecord, 100)

	lw := cache.NewListWatchFromClient(
		ec.clientset.CoreV1().RESTClient(),
		"events",
		ec.namespace,
		fields.OneTermEqualSelector("type", string(corev1.EventTypeWarning)),
	)

	_, ec.informer = cache.NewInformer(
		lw,
		&corev1.Event{},
		10*time.Minute, // resync period — periodic full re-list to prevent drift
		cache.ResourceEventHandlerFuncs{
			AddFunc: func(obj any) {
				ec.filterAndSend(obj, "ADDED", ch)
			},
			UpdateFunc: func(oldObj, newObj any) {
				ec.filterAndSend(newObj, "MODIFIED", ch)
			},
		},
	)

	go ec.informer.Run(ctx.Done())
	go func() {
		if cache.WaitForCacheSync(ctx.Done(), ec.informer.HasSynced) {
			ec.synced = true
			slog.Info("events: informer cache synced")
		} else {
			slog.Error("events: informer cache sync failed")
		}
		<-ctx.Done()
		close(ch)
	}()

	return ch
}

// HasSynced returns true once the informer has completed its initial list.
func (ec *EventsCollector) HasSynced() bool {
	return ec.synced
}

// filterAndSend converts a K8s event to an EventRecord, filters by failure reasons and
// namespace scope, then sends it to the channel with backpressure.
func (ec *EventsCollector) filterAndSend(obj any, eventType string, ch chan<- EventRecord) {
	k8sEvent, ok := obj.(*corev1.Event)
	if !ok {
		return
	}
	if !failureReasons[k8sEvent.Reason] {
		return
	}
	if !nsutil.InScope(k8sEvent.Namespace, ec.watchNamespaces) {
		return
	}

	record := EventRecord{
		Type:           eventType,
		Reason:         k8sEvent.Reason,
		Message:        k8sEvent.Message,
		Count:          k8sEvent.Count,
		InvolvedObject: k8sEvent.InvolvedObject.Name,
		Namespace:      k8sEvent.Namespace,
		Source:         k8sEvent.Source.Component,
		FirstTimestamp: k8sEvent.FirstTimestamp.Time,
		LastTimestamp:  k8sEvent.LastTimestamp.Time,
	}
	if k8sEvent.InvolvedObject.Kind != "" {
		record.InvolvedObject = k8sEvent.InvolvedObject.Kind + "/" + k8sEvent.InvolvedObject.Name
	}

	// Send with backpressure — drop if channel full after 5s
	timer := time.NewTimer(5 * time.Second)
	select {
	case ch <- record:
		if !timer.Stop() {
			<-timer.C
		}
	case <-timer.C:
		slog.Warn("events: channel full, timed out sending event", "reason", record.Reason, "object", record.InvolvedObject)
	}
}

// ListRecentEvents fetches recent Warning events without watching.
func (ec *EventsCollector) ListRecentEvents(ctx context.Context, since time.Duration) ([]EventRecord, error) {
	now := time.Now()
	opts := metav1.ListOptions{
		FieldSelector: fields.AndSelectors(
			fields.OneTermEqualSelector("type", string(corev1.EventTypeWarning)),
		).String(),
	}
	events, err := ec.clientset.CoreV1().Events(ec.namespace).List(ctx, opts)
	if err != nil {
		return nil, err
	}

	var records []EventRecord
	for _, e := range events.Items {
		if e.LastTimestamp.Time.Before(now.Add(-since)) {
			continue
		}
		if !failureReasons[e.Reason] {
			continue
		}
		involved := e.InvolvedObject.Name
		if e.InvolvedObject.Kind != "" {
			involved = e.InvolvedObject.Kind + "/" + e.InvolvedObject.Name
		}
		records = append(records, EventRecord{
			Type:           "Warning",
			Reason:         e.Reason,
			Message:        e.Message,
			Count:          e.Count,
			InvolvedObject: involved,
			Namespace:      e.Namespace,
			Source:         e.Source.Component,
			FirstTimestamp: e.FirstTimestamp.Time,
			LastTimestamp:  e.LastTimestamp.Time,
		})
	}
	return records, nil
}
