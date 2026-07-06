package collector

import (
	"context"
	"log"
	"math"
	"math/rand"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
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

// EventsCollector watches K8s events and emits failure-related records.
type EventsCollector struct {
	clientset kubernetes.Interface
	namespace string
}

// NewEventsCollector creates a new events collector.
func NewEventsCollector(clientset kubernetes.Interface, namespace string) *EventsCollector {
	return &EventsCollector{
		clientset: clientset,
		namespace: namespace,
	}
}

// WatchEvents starts watching events and returns a channel of EventRecords.
// It handles reconnection with exponential backoff.
func (ec *EventsCollector) WatchEvents(ctx context.Context) <-chan EventRecord {
	ch := make(chan EventRecord, 100)
	go ec.watchLoop(ctx, ch)
	return ch
}

func (ec *EventsCollector) watchLoop(ctx context.Context, ch chan<- EventRecord) {
	defer close(ch)

	rv := ""
	backoff := 1 * time.Second
	maxBackoff := 60 * time.Second

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		opts := metav1.ListOptions{
			FieldSelector: fields.AndSelectors(
				fields.OneTermEqualSelector("type", string(corev1.EventTypeWarning)),
			).String(),
			Watch: true,
		}
		if rv != "" {
			opts.ResourceVersion = rv
		}

		watcher, err := ec.clientset.CoreV1().Events(ec.namespace).Watch(ctx, opts)
		if err != nil {
			log.Printf("events: watch failed, retrying in %v: %v", backoff, err)
			if !sleepWithContext(ctx, backoff) {
				return
			}
			backoff = time.Duration(math.Min(float64(backoff*2), float64(maxBackoff)))
			backoff += time.Duration(rand.Int63n(int64(backoff / 4)))
			continue
		}

		backoff = 1 * time.Second

		for event := range watcher.ResultChan() {
			if event.Type == watch.Error {
				log.Printf("events: watch error: %v", event.Object)
				watcher.Stop()
				break
			}

			k8sEvent, ok := event.Object.(*corev1.Event)
			if !ok {
				continue
			}

			if !failureReasons[k8sEvent.Reason] {
				continue
			}

			record := EventRecord{
				Type:           string(event.Type),
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

			select {
			case ch <- record:
			default:
				log.Printf("events: channel full, dropping %s event for %s", record.Reason, record.InvolvedObject)
			}

			if event.Type == watch.Deleted || event.Type == watch.Modified {
				rv = k8sEvent.ResourceVersion
			}
		}

		if !sleepWithContext(ctx, backoff) {
			return
		}
		backoff = time.Duration(math.Min(float64(backoff*2), float64(maxBackoff)))
		backoff += time.Duration(rand.Int63n(int64(backoff / 4)))
	}
}

func sleepWithContext(ctx context.Context, d time.Duration) bool {
	select {
	case <-ctx.Done():
		return false
	case <-time.After(d):
		return true
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
