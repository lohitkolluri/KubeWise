package outcome

import (
	"fmt"
	"log"
	"math"
	"time"

	"github.com/lohitkolluri/KubeWise/internal/agent/store"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// entityData groups predictions and anomalies for a single resource.
type entityData struct {
	Predictions []models.TrackedPrediction
	Anomalies   []models.AnomalyRecord
}

// HealthComputer computes health scores from tracked predictions, anomalies,
// forecaster data, and remediation outcomes.
type HealthComputer struct {
	store *store.Store
}

// NewHealthComputer creates a health score computer.
func NewHealthComputer(s *store.Store) *HealthComputer {
	return &HealthComputer{store: s}
}

// ComputeAll computes health scores for all known entities and persists them.
func (hc *HealthComputer) ComputeAll() ([]models.HealthScore, error) {
	preds, err := hc.store.ListPredictionsInWindow(time.Now().Add(-7*24*time.Hour), 10000)
	if err != nil {
		return nil, err
	}

	anomalies, err := hc.store.ListAnomalies(5000)
	if err != nil {
		return nil, err
	}

	stats, err := hc.store.ComputeAgentStats()
	if err != nil {
		return nil, err
	}

	// Group tracked predictions by entity.
	groups := make(map[string]*entityData)

	for i := range preds {
		key := entityKey(preds[i].Namespace, preds[i].Entity)
		if groups[key] == nil {
			groups[key] = &entityData{}
		}
		groups[key].Predictions = append(groups[key].Predictions, preds[i])
	}
	for i := range anomalies {
		key := entityKey(anomalies[i].Namespace, anomalies[i].Entity)
		if groups[key] == nil {
			groups[key] = &entityData{}
		}
		groups[key].Anomalies = append(groups[key].Anomalies, anomalies[i])
	}

	now := time.Now()
	var scores []models.HealthScore

	for key, data := range groups {
		ns, entity := splitEntityKey(key)
		hs := hc.computeOne(ns, entity, data, stats, now)
		scores = append(scores, hs)
	}

	// Fetch previous scores for trend calculation.
	prev, err := hc.store.GetLatestHealthScores()
	if err == nil {
		prevMap := make(map[string]float64, len(prev))
		for _, p := range prev {
			prevMap[entityKey(p.Namespace, p.Entity)] = p.Score
		}
		for i := range scores {
			if ps, ok := prevMap[entityKey(scores[i].Namespace, scores[i].Entity)]; ok {
				scores[i].PreviousScore = ps
			}
			scores[i].Trend = classifyTrend(scores[i].Score, scores[i].PreviousScore)
		}
	}

	for _, hs := range scores {
		if err := hc.store.SaveHealthScore(hs); err != nil {
			log.Printf("health: save score for %s/%s: %v", hs.Namespace, hs.Entity, err)
		}
	}

	return scores, nil
}

// computeOne computes the health score for a single entity.
func (hc *HealthComputer) computeOne(namespace, entity string, data *entityData, stats models.AgentStats, now time.Time) models.HealthScore {
	factors := make([]models.ScoreFactor, 0, 4)

	// Factor 1: anomaly recency (weight 0.40)
	recency := anomalyRecencyFactor(data.Anomalies, now)
	factors = append(factors, models.ScoreFactor{
		Name:   "anomaly_recency",
		Weight: 0.40,
		Score:  recency,
		Detail: recencyDetail(data.Anomalies, now),
	})

	// Factor 2: forecast trend (weight 0.25) — neutral default
	forecast := 0.7
	factors = append(factors, models.ScoreFactor{
		Name:   "forecast_trend",
		Weight: 0.25,
		Score:  forecast,
		Detail: "forecaster trend: neutral (no forecast data for this entity)",
	})

	// Factor 3: remediation success rate (weight 0.20)
	remRate := remediationRateFactor(stats)
	factors = append(factors, models.ScoreFactor{
		Name:   "remediation_rate",
		Weight: 0.20,
		Score:  remRate,
		Detail: remediationDetail(stats),
	})

	// Factor 4: prediction confidence (weight 0.15)
	conf := predictionConfidenceFactor(data.Predictions)
	factors = append(factors, models.ScoreFactor{
		Name:   "confidence",
		Weight: 0.15,
		Score:  conf,
		Detail: confidenceDetail(data.Predictions),
	})

	// Weighted sum.
	var totalWeight, weightedSum float64
	for _, f := range factors {
		totalWeight += f.Weight
		weightedSum += f.Weight * f.Score
	}
	raw := (weightedSum / totalWeight) * 100
	score := math.Round(raw)

	resourceKind := "pod"
	if namespace == "" || entity == namespace {
		resourceKind = "namespace"
	}

	return models.HealthScore{
		Entity:       entity,
		Namespace:    namespace,
		ResourceKind: resourceKind,
		Score:        score,
		Factors:      factors,
		GeneratedAt:  now,
	}
}

// anomalyRecencyFactor returns 0-1 based on time since last anomaly.
func anomalyRecencyFactor(anomalies []models.AnomalyRecord, now time.Time) float64 {
	if len(anomalies) == 0 {
		return 1.0
	}
	// Find the most recent anomaly.
	var latest time.Time
	for _, a := range anomalies {
		if a.DetectedAt != nil && a.DetectedAt.After(latest) {
			latest = *a.DetectedAt
		}
	}
	if latest.IsZero() {
		return 1.0
	}
	minsAgo := now.Sub(latest).Minutes()
	switch {
	case minsAgo <= 5:
		return 0.0
	case minsAgo >= 7*24*60: // 7 days
		return 1.0
	default:
		// Linear decay from 5min to 7d.
		return 1.0 - (minsAgo-5)/(7*24*60-5)
	}
}

// remediationRateFactor returns 0-1 based on remediation success rate.
func remediationRateFactor(stats models.AgentStats) float64 {
	total := stats.RemediationsVerified + stats.RemediationsFailed
	if total == 0 {
		return 0.8 // conservative default
	}
	return float64(stats.RemediationsVerified) / float64(total)
}

// predictionConfidenceFactor returns 0-1 based on recent prediction accuracy.
func predictionConfidenceFactor(preds []models.TrackedPrediction) float64 {
	var hits, misses int
	for _, tp := range preds {
		switch tp.Outcome {
		case models.PredictionOutcomeHit:
			hits++
		case models.PredictionOutcomeMiss:
			misses++
		}
	}
	total := hits + misses
	if total == 0 {
		return 0.7
	}
	return float64(hits) / float64(total)
}

// classifyTrend determines the trend direction from score delta.
func classifyTrend(current, previous float64) string {
	if current < 20 {
		return "critical"
	}
	delta := current - previous
	switch {
	case delta > 5:
		return "improving"
	case delta < -5:
		return "degrading"
	default:
		return "stable"
	}
}

// entityKey produces a composite key for grouping.
func entityKey(namespace, entity string) string {
	if namespace == "" {
		return entity
	}
	return namespace + "/" + entity
}

// splitEntityKey splits a composite key back into namespace and entity.
func splitEntityKey(key string) (string, string) {
	for i := len(key) - 1; i >= 0; i-- {
		if key[i] == '/' {
			return key[:i], key[i+1:]
		}
	}
	return "", key
}

// recencyDetail builds a human-readable string for the anomaly recency factor.
func recencyDetail(anomalies []models.AnomalyRecord, now time.Time) string {
	if len(anomalies) == 0 {
		return "no anomalies in 7d"
	}
	var latest time.Time
	for _, a := range anomalies {
		if a.DetectedAt != nil && a.DetectedAt.After(latest) {
			latest = *a.DetectedAt
		}
	}
	if latest.IsZero() {
		return "no anomalies in 7d"
	}
	minsAgo := int(now.Sub(latest).Minutes())
	return fmtAnomalySummary(len(anomalies), minsAgo)
}

func fmtAnomalySummary(count, minsAgo int) string {
	if count == 1 {
		return fmt.Sprintf("1 anomaly, last %dmin ago", minsAgo)
	}
	return fmt.Sprintf("%d anomalies, last %dmin ago", count, minsAgo)
}

// remediationDetail builds a human-readable string for remediation rate.
func remediationDetail(stats models.AgentStats) string {
	total := stats.RemediationsVerified + stats.RemediationsFailed
	if total == 0 {
		return "no remediation data"
	}
	return fmt.Sprintf("%d/%d remediations successful", stats.RemediationsVerified, total)
}

// confidenceDetail builds a human-readable string for prediction confidence.
func confidenceDetail(preds []models.TrackedPrediction) string {
	var hits, misses int
	for _, tp := range preds {
		switch tp.Outcome {
		case models.PredictionOutcomeHit:
			hits++
		case models.PredictionOutcomeMiss:
			misses++
		}
	}
	total := hits + misses
	if total == 0 {
		return "no resolved predictions in 7d"
	}
	pct := int(float64(hits) / float64(total) * 100)
	return fmt.Sprintf("%d/%d predictions correct (%d%%)", hits, total, pct)
}
