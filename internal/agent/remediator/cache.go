package remediator

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"

	"github.com/lohitkolluri/KubeWise/internal/agent/semcache"
	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// lookupCachedPlan checks the semantic cache for a matching anomaly fingerprint.
// Returns a deserialized RemediationPlan on cache hit, nil otherwise.
func (c *Correlator) lookupCachedPlan(anomalies []models.AnomalyRecord) *models.RemediationPlan {
	fp := fingerprintAnomalyGroup(anomalies)
	if fp == "" {
		return nil
	}
	entry := c.semCache.Get(fp)
	if entry == nil {
		return nil
	}
	var plan models.RemediationPlan
	if err := json.Unmarshal([]byte(entry.PlanJSON), &plan); err != nil {
		slog.Error("remediator: semcache deserialize error", "error", err)
		return nil
	}
	return &plan
}

// storeCachedPlan serializes and stores a RemediationPlan in the semantic cache.
func (c *Correlator) storeCachedPlan(anomalies []models.AnomalyRecord, plan models.RemediationPlan) {
	fp := fingerprintAnomalyGroup(anomalies)
	if fp == "" {
		return
	}
	data, err := json.Marshal(plan)
	if err != nil {
		slog.Error("remediator: semcache serialize error", "error", err)
		return
	}
	c.semCache.Set(fp, string(data))
}

// fingerprintAnomalyGroup computes a combined fingerprint for a group of anomalies.
// Uses a combined SHA256 hash of all individual anomaly fingerprints.
func fingerprintAnomalyGroup(anomalies []models.AnomalyRecord) string {
	if len(anomalies) == 0 {
		return ""
	}
	if len(anomalies) == 1 {
		a := anomalies[0]
		return semcache.Fingerprint(a.Entity, a.Namespace, a.MetricName, a.Pattern, a.Score)
	}
	h := sha256.New()
	for _, a := range anomalies {
		fp := semcache.Fingerprint(a.Entity, a.Namespace, a.MetricName, a.Pattern, a.Score)
		h.Write([]byte(fp))
		h.Write([]byte{0})
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}
