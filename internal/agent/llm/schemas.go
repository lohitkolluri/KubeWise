package llm

import "encoding/json"

// RemediationSchema returns the JSON Schema for the remediation_plan structured output.
func RemediationSchema() json.RawMessage {
	schema := `{
		"type": "object",
		"properties": {
			"diagnosis": {
				"type": "object",
				"properties": {
					"root_cause": { "type": "string", "description": "Root cause of the anomaly" },
					"severity": { "type": "string", "enum": ["critical", "warning", "info"], "description": "Severity level" },
					"confidence": { "type": "number", "description": "Confidence in the diagnosis, 0.0 to 1.0" },
					"evidence": { "type": "array", "items": { "type": "string" }, "description": "Specific evidence supporting the diagnosis — cite logs, events, and metrics" }
				},
				"required": ["root_cause", "severity", "confidence", "evidence"],
				"additionalProperties": false
			},
			"action": {
				"type": "object",
				"properties": {
					"type": {
						"type": "string",
						"enum": ["restart_pod", "scale_replicas", "rollback_deployment", "patch_resources", "delete_pod", "escalate", "noop"],
						"description": "Primary action (first step) for backward compatibility"
					},
					"target": { "type": "string", "description": "Name of the target resource" },
					"namespace": { "type": "string", "description": "Kubernetes namespace of the target" },
					"parameters": { "type": "object", "additionalProperties": { "type": "string" }, "description": "Optional action parameters" },
					"rationale": { "type": "string", "description": "Why this action addresses the root cause" }
				},
				"required": ["type", "target", "namespace", "rationale"],
				"additionalProperties": false
			},
			"steps": {
				"type": "array",
				"description": "Ordered multi-step runbook (max 5 steps). Use when one action is insufficient.",
				"items": {
					"type": "object",
					"properties": {
						"order": { "type": "integer", "description": "Step number starting at 1" },
						"type": {
							"type": "string",
							"enum": ["restart_pod", "scale_replicas", "rollback_deployment", "patch_resources", "delete_pod", "wait", "escalate", "noop"],
							"description": "Step action type; use wait to pause between steps"
						},
						"target": { "type": "string" },
						"namespace": { "type": "string" },
						"parameters": { "type": "object", "additionalProperties": { "type": "string" } },
						"rationale": { "type": "string" },
						"wait_seconds": { "type": "integer", "description": "Pause after this step completes" }
					},
					"required": ["order", "type", "target", "namespace", "rationale"],
					"additionalProperties": false
				}
			},
			"verification": {
				"type": "object",
				"description": "Post-remediation checks to confirm the issue is resolved",
				"properties": {
					"wait_seconds": { "type": "integer", "description": "Seconds to wait before running checks (default 15)" },
					"checks": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {
								"type": {
									"type": "string",
									"enum": ["pod_ready", "pod_phase", "no_crashloop", "deployment_ready"],
									"description": "Verification check type"
								},
								"target": { "type": "string" },
								"namespace": { "type": "string" },
								"parameters": { "type": "object", "additionalProperties": { "type": "string" } }
							},
							"required": ["type", "target", "namespace"],
							"additionalProperties": false
						}
					}
				},
				"required": ["wait_seconds", "checks"],
				"additionalProperties": false
			},
			"risk": {
				"type": "object",
				"properties": {
					"blast_radius": { "type": "string", "enum": ["single_pod", "multiple_pods", "service", "cluster"], "description": "How widely the action impacts the cluster" },
					"reversible": { "type": "boolean", "description": "Whether the action can be easily undone" },
					"estimated_time_to_resolve": { "type": "string", "description": "Estimated time to resolution, e.g. '30s', '2m'" }
				},
				"required": ["blast_radius", "reversible", "estimated_time_to_resolve"],
				"additionalProperties": false
			}
		},
		"required": ["diagnosis", "action", "steps", "verification", "risk"],
		"additionalProperties": false
	}`
	return json.RawMessage(schema)
}
