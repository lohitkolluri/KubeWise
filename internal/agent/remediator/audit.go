package remediator

import (
	"crypto/rand"
	"fmt"
	"log/slog"
	"math/big"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

func (c *Correlator) logAuditVerified(plan *models.RemediationPlan, anomalies []models.AnomalyRecord, tier models.RiskTier, reason, prompt, k8sResult, verifyNote string, verifiedAt *time.Time) {
	now := time.Now()
	anomalyIDs := make([]string, 0, len(anomalies))
	for _, a := range anomalies {
		anomalyIDs = append(anomalyIDs, a.ID)
	}
	record := &models.AuditRecord{
		ID:               shortID(),
		AnomalyIDs:       anomalyIDs,
		Plan:             *plan,
		RiskTier:         tier,
		Status:           models.AuditVerified,
		Reason:           reason,
		Prompt:           prompt,
		LLMResponse:      fmt.Sprintf("%+v", *plan),
		K8sResult:        k8sResult,
		VerificationNote: verifyNote,
		CreatedAt:        now,
		ExecutedAt:       verifiedAt,
		VerifiedAt:       verifiedAt,
	}
	if err := c.store.SaveAuditRecord(record); err != nil {
		slog.Error("remediator: failed to save audit record", "error", err)
		return
	}
	c.emitAuditNotification(record)
}

func (c *Correlator) logAudit(plan *models.RemediationPlan, anomalies []models.AnomalyRecord, tier models.RiskTier, status models.AuditStatus, reason, prompt, k8sResult string) {
	now := time.Now()
	anomalyIDs := make([]string, 0, len(anomalies))
	for _, a := range anomalies {
		anomalyIDs = append(anomalyIDs, a.ID)
	}
	record := &models.AuditRecord{
		ID:               shortID(),
		AnomalyID:        "",
		AnomalyIDs:       anomalyIDs,
		Plan:             *plan,
		RiskTier:         tier,
		Status:           status,
		Reason:           reason,
		Prompt:           prompt,
		LLMResponse:      fmt.Sprintf("%+v", *plan),
		K8sResult:        k8sResult,
		VerificationNote: "",
		CreatedAt:        now,
	}

	if status == models.AuditExecuted {
		record.ExecutedAt = &now
	}

	if err := c.store.SaveAuditRecord(record); err != nil {
		slog.Error("remediator: failed to save audit record", "error", err)
		return
	}
	c.emitAuditNotification(record)
}

func shortID() string {
	const chars = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, 12)
	for i := range b {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(len(chars))))
		if err != nil {
			b[i] = chars[i%len(chars)]
			continue
		}
		b[i] = chars[n.Int64()]
	}
	now := time.Now().UnixMilli()
	return fmt.Sprintf("%x-%s", now, string(b))
}
