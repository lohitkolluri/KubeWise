package models

import "time"

// RemediationAction represents an automated remediation action taken or planned.
type RemediationAction struct {
	ID          string            `json:"id"`
	Type        string            `json:"type"`
	Target      string            `json:"target"`
	Status      string            `json:"status"`
	Params      map[string]string `json:"params,omitempty"`
	ExecutedAt  *time.Time        `json:"executed_at,omitempty"`
	VerifiedAt  *time.Time        `json:"verified_at,omitempty"`
	Error       string            `json:"error,omitempty"`
}
