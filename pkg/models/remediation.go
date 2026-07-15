package models

import "time"

// ToolCapability describes what a tool can do.
type ToolCapability string

const (
	// CapRead indicates the tool can read cluster state.
	CapRead ToolCapability = "read"
	// CapWrite indicates the tool can modify cluster state.
	CapWrite ToolCapability = "write"
	// CapDestructive indicates the tool performs irreversible destructive actions.
	CapDestructive ToolCapability = "destructive"
	// CapRequiresApproval indicates the tool needs explicit human approval.
	CapRequiresApproval ToolCapability = "requires_approval"
)

// ToolAction is a request to execute a specific tool operation.
// Used by the tool plugin system and serialized in audit/API responses.
type ToolAction struct {
	Tool    string            `json:"tool"`
	Command string            `json:"command"`
	Args    map[string]string `json:"args,omitempty"`
	Timeout DurationValue     `json:"timeout,omitempty"`
}

// ToolResult is what the tool returned after execution.
type ToolResult struct {
	Success  bool              `json:"success"`
	Stdout   string            `json:"stdout"`
	Stderr   string            `json:"stderr"`
	ExitCode int               `json:"exit_code,omitempty"`
	Duration DurationValue     `json:"duration"`
	Metadata map[string]string `json:"metadata,omitempty"`
}

// RiskTier represents the severity/risk level of a remediation action.
type RiskTier string

const (
	// RiskTier1 represents auto-execute remediation (safe actions like pod restart).
	RiskTier1 RiskTier = "T1" // Auto-execute
	// RiskTier2 represents cooldown-gated remediation (e.g., scaling or rolling update).
	RiskTier2 RiskTier = "T2" // Cooldown-gated
	// RiskTier3 represents remediation that needs human approval (escalate).
	RiskTier3 RiskTier = "T3" // Needs human approval (escalate)
	// RiskTier4 represents actions that are always rejected (unknown or cluster-wide).
	RiskTier4 RiskTier = "T4" // Always rejected
)

// RunbookStep is one ordered remediation action in a multi-step runbook.
type RunbookStep struct {
	Order       int               `json:"order"`
	Type        string            `json:"type"`
	Target      string            `json:"target"`
	Namespace   string            `json:"namespace"`
	Parameters  map[string]string `json:"parameters,omitempty"`
	Rationale   string            `json:"rationale"`
	WaitSeconds int               `json:"wait_seconds,omitempty"`
}

// VerificationCheck asserts post-remediation health.
type VerificationCheck struct {
	Type       string            `json:"type"`
	Target     string            `json:"target"`
	Namespace  string            `json:"namespace"`
	Parameters map[string]string `json:"parameters,omitempty"`
}

// VerificationPlan describes how to confirm remediation worked.
type VerificationPlan struct {
	Checks      []VerificationCheck `json:"checks"`
	WaitSeconds int                 `json:"wait_seconds,omitempty"`
}

// InvestigationContext is gathered from the cluster before LLM analysis (not LLM output).
type InvestigationContext struct {
	Summary string `json:"summary"`
}

// RemediationPlan is the structured output from the LLM correlator.
type RemediationPlan struct {
	Diagnosis     Diagnosis            `json:"diagnosis"`
	Action        Action               `json:"action"`
	Steps         []RunbookStep        `json:"steps,omitempty"`
	Verification  VerificationPlan     `json:"verification,omitempty"`
	Investigation InvestigationContext `json:"investigation,omitempty"`
	Risk          Risk                 `json:"risk"`
}

// EffectiveSteps returns runbook steps, falling back to the legacy single action.
func (p RemediationPlan) EffectiveSteps() []RunbookStep {
	if len(p.Steps) > 0 {
		return p.Steps
	}
	if p.Action.Type == "" {
		return nil
	}
	return []RunbookStep{{
		Order:      1,
		Type:       p.Action.Type,
		Target:     p.Action.Target,
		Namespace:  p.Action.Namespace,
		Parameters: p.Action.Parameters,
		Rationale:  p.Action.Rationale,
	}}
}

// StepToAction converts a runbook step to a legacy Action.
func StepToAction(s RunbookStep) Action {
	return Action{
		Type:       s.Type,
		Target:     s.Target,
		Namespace:  s.Namespace,
		Parameters: s.Parameters,
		Rationale:  s.Rationale,
	}
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
	Params     map[string]string `json:"parameters,omitempty"`
	ExecutedAt *time.Time        `json:"executed_at,omitempty"`
	VerifiedAt *time.Time        `json:"verified_at,omitempty"`
	Error      string            `json:"error,omitempty"`
}

// AuditStatus describes the outcome of a remediation decision.
type AuditStatus string

const (
	// AuditApproved indicates a remediation plan was approved for execution.
	AuditApproved AuditStatus = "approved"
	// AuditRejected indicates a remediation plan was rejected.
	AuditRejected AuditStatus = "rejected"
	// AuditExecuted indicates a remediation plan was executed.
	AuditExecuted AuditStatus = "executed"
	// AuditFailed indicates a remediation plan execution failed.
	AuditFailed AuditStatus = "failed"
	// AuditDryRun indicates a remediation plan was tested without execution.
	AuditDryRun AuditStatus = "dry_run"
	// AuditEscalated indicates a remediation plan was escalated to a higher tier.
	AuditEscalated AuditStatus = "escalated"
	// AuditPending indicates a remediation plan is awaiting approval.
	AuditPending AuditStatus = "pending_approval"
	// AuditVerified indicates a remediation plan was verified as successful.
	AuditVerified AuditStatus = "verified"
	// AuditVerifyFailed indicates post-remediation verification failed.
	AuditVerifyFailed AuditStatus = "verify_failed"
)

// AuditRecord stores every remediation decision for observability.
type AuditRecord struct {
	ID               string          `json:"id"`
	AnomalyID        string          `json:"anomaly_id,omitempty"`
	AnomalyIDs       []string        `json:"anomaly_ids,omitempty"`
	Plan             RemediationPlan `json:"plan"`
	RiskTier         RiskTier        `json:"risk_tier"`
	Status           AuditStatus     `json:"status"`
	Reason           string          `json:"reason,omitempty"`
	Prompt           string          `json:"prompt,omitempty"`
	LLMResponse      string          `json:"llm_response,omitempty"`
	K8sResult        string          `json:"k8s_result,omitempty"`
	VerificationNote string          `json:"verification_note,omitempty"`
	CreatedAt        time.Time       `json:"created_at"`
	ExecutedAt       *time.Time      `json:"executed_at,omitempty"`
	VerifiedAt       *time.Time      `json:"verified_at,omitempty"`
	Error            string          `json:"error,omitempty"`
}

// CooldownState tracks per-(namespace, action) cooldowns for T2 actions.
type CooldownState struct {
	Namespace string    `json:"namespace"`
	Action    string    `json:"action"`
	Until     time.Time `json:"until"`
}
