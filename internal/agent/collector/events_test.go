package collector

import (
	"context"
	"os"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
)

func TestNewEventsCollector(t *testing.T) {
	f, err := os.CreateTemp("", "kw-store-*.db")
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	defer os.Remove(f.Name())

	s, err := store.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	cs := fake.NewSimpleClientset()
	ec := NewEventsCollector(cs, "", s)
	if ec == nil {
		t.Fatal("expected non-nil EventsCollector")
	}
}

func TestListRecentEvents(t *testing.T) {
	cs := fake.NewSimpleClientset(
		&corev1.Event{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-event-1",
				Namespace: "default",
			},
			Type:    "Warning",
			Reason:  "BackOff",
			Message: "Back-off 5s restarting container",
			InvolvedObject: corev1.ObjectReference{
				Kind: "Pod",
				Name: "nginx-abc",
			},
			LastTimestamp: metav1.NewTime(time.Now()),
			Source:        corev1.EventSource{Component: "kubelet"},
		},
		&corev1.Event{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "normal-event",
				Namespace: "default",
			},
			Type:    "Normal",
			Reason:  "Pulled",
			Message: "Container image pulled",
			InvolvedObject: corev1.ObjectReference{
				Kind: "Pod",
				Name: "nginx-abc",
			},
			LastTimestamp: metav1.NewTime(time.Now()),
		},
	)

	f, err := os.CreateTemp("", "kw-store-*.db")
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	defer os.Remove(f.Name())

	s, err := store.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	ec := NewEventsCollector(cs, "", s)
	records, err := ec.ListRecentEvents(context.Background(), 1*time.Hour)
	if err != nil {
		t.Fatalf("ListRecentEvents: %v", err)
	}
	if len(records) != 1 {
		t.Fatalf("expected 1 failure event, got %d", len(records))
	}
	if records[0].Reason != "BackOff" {
		t.Fatalf("expected BackOff reason, got %s", records[0].Reason)
	}
}

func TestListRecentEvents_FiltersOldEvents(t *testing.T) {
	cs := fake.NewSimpleClientset(
		&corev1.Event{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "old-event",
				Namespace: "default",
			},
			Type:    "Warning",
			Reason:  "BackOff",
			Message: "Old back-off",
			InvolvedObject: corev1.ObjectReference{
				Kind: "Pod",
				Name: "old-pod",
			},
			LastTimestamp: metav1.NewTime(time.Now().Add(-2 * time.Hour)),
		},
	)

	f, err := os.CreateTemp("", "kw-store-*.db")
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	defer os.Remove(f.Name())

	s, err := store.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	ec := NewEventsCollector(cs, "", s)
	records, err := ec.ListRecentEvents(context.Background(), 1*time.Hour)
	if err != nil {
		t.Fatalf("ListRecentEvents: %v", err)
	}
	if len(records) != 0 {
		t.Fatalf("expected 0 old events, got %d", len(records))
	}
}

func TestFailureReasons(t *testing.T) {
	expected := []string{
		"BackOff", "OOMKilling", "FailedMount", "ImagePull",
		"ImagePullBackOff", "CrashLoopBackOff", "ProbeError",
		"NodeCondition", "NodeNotReady", "Evicted",
		"FailedPlacement", "FailedScheduling", "FailedNodeAffinity",
		"OutOfDisk", "MemoryPressure", "DiskPressure",
	}
	for _, reason := range expected {
		if !failureReasons[reason] {
			t.Errorf("expected %q to be in failureReasons", reason)
		}
	}
}
