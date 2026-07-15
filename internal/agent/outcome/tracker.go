package outcome

import (
	"crypto/rand"
	"fmt"
	"math/big"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/predictor"
	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// Tracker records pattern predictions and verifies outcomes against cluster state.
type Tracker struct {
	store *store.Store
}

// NewTracker creates a prediction outcome tracker.
func NewTracker(s *store.Store) *Tracker {
	return &Tracker{store: s}
}

// TrackPatternPredictions opens tracking records for high-confidence pattern predictions.
func (t *Tracker) TrackPatternPredictions(preds []models.PredictionResult, now time.Time) {
	if t == nil || t.store == nil {
		return
	}
	for _, p := range preds {
		if p.Type != "pattern" || p.Confidence < 0.6 {
			continue
		}
		entity := p.Entity
		if p.Namespace != "" {
			entity = models.FormatEntity(p.Namespace, p.Entity)
		}
		eta := p.ETASeconds
		if eta <= 0 {
			eta = 600
		}
		tp := models.TrackedPrediction{
			ID:         shortID(),
			Entity:     entity,
			Namespace:  p.Namespace,
			Pattern:    p.MetricName,
			MetricName: p.MetricName,
			Confidence: p.Confidence,
			ETASeconds: eta,
			CreatedAt:  now,
			ExpiresAt:  now.Add(time.Duration(eta*2) * time.Second),
			Outcome:    models.PredictionOutcomePending,
		}
		_ = t.store.SaveTrackedPrediction(tp)
	}
}

// VerifyPending checks open predictions against the current resource snapshot.
func (t *Tracker) VerifyPending(resources predictor.ResourceSnapshot, now time.Time) {
	if t == nil || t.store == nil {
		return
	}
	_, _ = t.store.PruneExpiredPredictions(now)

	pending, err := t.store.ListTrackedPredictions(models.PredictionOutcomePending, 500)
	if err != nil {
		return
	}

	failingSet := make(map[string]struct{})
	for _, e := range resources.FailingPods {
		failingSet[e] = struct{}{}
	}

	for _, tp := range pending {
		hit := predictionHit(tp, failingSet, resources)
		if !hit {
			continue
		}
		tp.Outcome = models.PredictionOutcomeHit
		tp.ResolvedAt = &now
		_ = t.store.UpdateTrackedPrediction(tp)
	}
}

func predictionHit(tp models.TrackedPrediction, failing map[string]struct{}, resources predictor.ResourceSnapshot) bool {
	entity := tp.Entity
	if _, ok := failing[entity]; ok {
		return true
	}

	// Owner-aware matching: the predicted pod may have been restarted with a new name.
	// Try inferring the owner (deployment/statefulset) from the entity.
	_, podName := models.ParseEntity(entity)
	owner := inferDeploymentFromPodName(podName)
	if owner != "" {
		for failingEntity := range failing {
			fNs, fName := models.ParseEntity(failingEntity)
			if fNs == tp.Namespace || (tp.Namespace == "" && fNs == "") {
				if podBelongsToDeployment(fName, owner) {
					return true
				}
			}
		}
		// Also check PodResources for matching owner
		for _, p := range resources.PodResources {
			e := models.FormatEntity(p.Namespace, p.Name)
			if _, ok := failing[e]; !ok {
				continue
			}
			if podBelongsToDeployment(p.Name, owner) {
				return true
			}
		}
	}

	pattern := strings.ToLower(tp.Pattern)
	switch {
	case strings.Contains(pattern, "oom"), strings.Contains(pattern, "memory"):
		for _, p := range resources.PodResources {
			e := models.FormatEntity(p.Namespace, p.Name)
			if e != entity {
				continue
			}
			if _, ok := failing[e]; ok {
				return true
			}
		}
	case strings.Contains(pattern, "crash"), strings.Contains(pattern, "restart"):
		if _, ok := failing[entity]; ok {
			return true
		}
	}

	return false
}

// inferDeploymentFromPodName attempts to derive the owner name from a pod name.
// Duplicated from remediator package since outcome can't import it.
func inferDeploymentFromPodName(pod string) string {
	if pod == "" {
		return ""
	}
	parts := strings.Split(pod, "-")
	if len(parts) < 2 {
		return ""
	}
	if len(parts) >= 3 {
		return strings.Join(parts[:len(parts)-2], "-")
	}
	return parts[0] // 2 segments: treat first segment as owner name (DaemonSet/StatefulSet)
}

// podBelongsToDeployment checks if a pod name belongs to a deployment/owner.
func podBelongsToDeployment(podName, deployment string) bool {
	if deployment == "" || podName == "" {
		return false
	}
	parts := strings.Split(podName, "-")
	for i := len(parts) - 1; i >= 1; i-- {
		if strings.Join(parts[:i], "-") == deployment {
			return true
		}
	}
	return false
}

func shortID() string {
	const chars = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, 8)
	for i := range b {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(len(chars))))
		if err != nil {
			b[i] = chars[i%len(chars)]
			continue
		}
		b[i] = chars[n.Int64()]
	}
	return fmt.Sprintf("pred-%d-%s", time.Now().UnixMilli(), string(b))
}
