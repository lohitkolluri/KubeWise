package models

import "time"

// RiskTier represents the severity/risk level of a remediation action.
type RiskTier string

const (
	RiskTier1 RiskTier = "T1" // Auto-execute
	RiskTier2 RiskTier = "T2" // Cooldown-gated
	RiskTier3 RiskTier = "T3" // Needs human approval (escalate)
	RiskTier4 RiskTier = "T4" // Always rejected
)

// RemediationPlan is the structured output from the LLM correlator.
type RemediationPlan struct {
	Diagnosis Diagnosis `json:"diagnosis"`
	Action    Action    `json:"action"`
	Risk      Risk      `json:"risk"`
}

// Diagnosis describes what the LLM believes is wrong.
type Diagnosis struct {
	RootCause  string   `json:"root_cause"`
	Severity   string   `json:"severity"`
	Confidence float64  `json:"confidence"`
	Evidence   []string `json:"evidence"`
}

// Action describes the remediation action to take.
type Action struct {
	Type       string            `json:"type"`
	Target     string            `json:"target"`
	Namespace  string            `json:"namespace"`
	Parameters map[string]string `json:"parameters,omitempty"`
	Rationale  string            `json:"rationale"`
}

// Risk describes the blast radius and reversibility of the action.
type Risk struct {
	BlastRadius            string `json:"blast_radius"`
	Reversible             bool   `json:"reversible"`
	EstimatedTimeToResolve string `json:"estimated_time_to_resolve"`
}

// RemediationAction represents an automated remediation action taken or planned.
type RemediationAction struct {
	ID         string            `json:"id"`
	Type       string            `json:"type"`
	Target     string            `json:"target"`
	Status     string            `json:"status"`
	Params     map[string]string `json:"params,omitempty"`
	ExecutedAt *time.Time        `json:"executed_at,omitempty"`
	VerifiedAt *time.Time        `json:"verified_at,omitempty"`
	Error      string            `json:"error,omitempty"`
}

// AuditStatus describes the outcome of a remediation decision.
type AuditStatus string

const (
	AuditApproved  AuditStatus = "approved"
	AuditRejected  AuditStatus = "rejected"
	AuditExecuted  AuditStatus = "executed"
	AuditFailed    AuditStatus = "failed"
	AuditDryRun    AuditStatus = "dry_run"
	AuditEscalated AuditStatus = "escalated"
	AuditPending   AuditStatus = "pending_approval"
)

// AuditRecord stores every remediation decision for observability.
type AuditRecord struct {
	ID          string          `json:"id"`
	AnomalyID   string          `json:"anomaly_id,omitempty"`
	AnomalyIDs  []string        `json:"anomaly_ids,omitempty"`
	Plan        RemediationPlan `json:"plan"`
	RiskTier    RiskTier        `json:"risk_tier"`
	Status      AuditStatus     `json:"status"`
	Reason      string          `json:"reason,omitempty"`
	Prompt      string          `json:"prompt,omitempty"`
	LLMResponse string          `json:"llm_response,omitempty"`
	K8sResult   string          `json:"k8s_result,omitempty"`
	CreatedAt   time.Time       `json:"created_at"`
	ExecutedAt  *time.Time      `json:"executed_at,omitempty"`
	VerifiedAt  *time.Time      `json:"verified_at,omitempty"`
	Error       string          `json:"error,omitempty"`
}

// CooldownState tracks per-(namespace, action) cooldowns for T2 actions.
type CooldownState struct {
	Namespace string    `json:"namespace"`
	Action    string    `json:"action"`
	Until     time.Time `json:"until"`
}
