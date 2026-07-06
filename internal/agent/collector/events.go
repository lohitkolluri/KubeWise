package collector

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
)

// failureReasons lists K8s event reasons that indicate potential failures.
var failureReasons = map[string]bool{
	"BackOff":           true,
	"OOMKilling":        true,
	"FailedMount":       true,
	"ImagePull":         true,
	"ImagePullBackOff":  true,
	"CrashLoopBackOff":  true,
	"ProbeError":        true,
	"NodeCondition":     true,
	"NodeNotReady":      true,
	"Evicted":           true,
	"FailedPlacement":   true,
	"FailedScheduling":  true,
	"FailedNodeAffinity": true,
	"OutOfDisk":         true,
	"MemoryPressure":    true,
	"DiskPressure":      true,
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
	store     *store.Store
	namespace string
}

// NewEventsCollector creates a new events collector.
func NewEventsCollector(clientset kubernetes.Interface, namespace string, s *store.Store) *EventsCollector {
	return &EventsCollector{
		clientset: clientset,
		store:     s,
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
			FieldSelector:   fields.AndSelectors(
				fields.OneTermEqualSelector("type", string(corev1.EventTypeWarning)),
			).String(),
			Watch:           true,
		}
		if rv != "" {
			opts.ResourceVersion = rv
		}

		watcher, err := ec.clientset.CoreV1().Events(ec.namespace).Watch(ctx, opts)
		if err != nil {
			fmt.Printf("events: watch failed, retrying in %v: %v\n", backoff, err)
			time.Sleep(backoff)
			backoff = time.Duration(math.Min(float64(backoff*2), float64(maxBackoff)))
			// Add jitter
			backoff += time.Duration(rand.Int63n(int64(backoff / 4)))
			continue
		}

		backoff = 1 * time.Second // Reset on successful connection

		for event := range watcher.ResultChan() {
			if event.Type == watch.Error {
				fmt.Printf("events: watch error: %v\n", event.Object)
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
				InvolvedObject: fmt.Sprintf("%s/%s", k8sEvent.InvolvedObject.Kind, k8sEvent.InvolvedObject.Name),
				Namespace:      k8sEvent.Namespace,
				Source:         k8sEvent.Source.Component,
				FirstTimestamp: k8sEvent.FirstTimestamp.Time,
				LastTimestamp:  k8sEvent.LastTimestamp.Time,
			}

			// Emit on channel for the agent to gate and persist.
			select {
			case ch <- record:
			default:
				// Channel full, drop event
			}

			if event.Type == watch.Deleted || event.Type == watch.Modified {
				rv = k8sEvent.ResourceVersion
			}
		}

		// Watch ended, reconnect
		time.Sleep(backoff)
		backoff = time.Duration(math.Min(float64(backoff*2), float64(maxBackoff)))
		backoff += time.Duration(rand.Int63n(int64(backoff / 4)))
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
		return nil, fmt.Errorf("list events: %w", err)
	}

	var records []EventRecord
	for _, e := range events.Items {
		if e.LastTimestamp.Time.Before(now.Add(-since)) {
			continue
		}
		if !failureReasons[e.Reason] {
			continue
		}
		records = append(records, EventRecord{
			Type:           "Warning",
			Reason:         e.Reason,
			Message:        e.Message,
			Count:          e.Count,
			InvolvedObject: fmt.Sprintf("%s/%s", e.InvolvedObject.Kind, e.InvolvedObject.Name),
			Namespace:      e.Namespace,
			Source:         e.Source.Component,
			FirstTimestamp: e.FirstTimestamp.Time,
			LastTimestamp:  e.LastTimestamp.Time,
		})
	}
	return records, nil
}
