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
				"description": "Primary action (must mirror first non-wait step for compatibility).",
				"oneOf": [
					{
						"type": "object",
						"properties": {
							"type": { "const": "patch_resources" },
							"target": { "type": "string" },
							"namespace": { "type": "string" },
							"parameters": {
								"type": "object",
								"description": "For patch_resources, include at least one resource field.",
								"properties": {
									"cpu_request": { "type": "string" },
									"cpu_limit": { "type": "string" },
									"memory_request": { "type": "string" },
									"memory_limit": { "type": "string" }
								},
								"minProperties": 1,
								"additionalProperties": false
							},
							"rationale": { "type": "string" }
						},
						"required": ["type", "target", "namespace", "parameters", "rationale"],
						"additionalProperties": false
					},
					{
						"type": "object",
						"properties": {
							"type": { "const": "scale_replicas" },
							"target": { "type": "string" },
							"namespace": { "type": "string" },
							"parameters": {
								"type": "object",
								"properties": {
									"replicas": { "type": "string", "description": "Desired replica count (integer as string)" },
									"deployment": { "type": "string", "description": "Optional deployment name when target is a pod" }
								},
								"required": ["replicas"],
								"additionalProperties": false
							},
							"rationale": { "type": "string" }
						},
						"required": ["type", "target", "namespace", "parameters", "rationale"],
						"additionalProperties": false
					},
					{
						"type": "object",
						"properties": {
							"type": {
								"type": "string",
								"enum": ["restart_pod", "rollback_deployment", "delete_pod", "escalate", "noop"],
								"description": "Primary action (first step) for backward compatibility"
							},
							"target": { "type": "string", "description": "Name of the target resource" },
							"namespace": { "type": "string", "description": "Kubernetes namespace of the target" },
							"parameters": { "type": "object", "additionalProperties": { "type": "string" }, "description": "Optional action parameters", "default": {} },
							"rationale": { "type": "string", "description": "Why this action addresses the root cause" }
						},
						"required": ["type", "target", "namespace", "rationale"],
						"additionalProperties": false
					}
				]
			},
			"steps": {
				"type": "array",
				"description": "Ordered multi-step runbook (max 5 steps). Use when one action is insufficient.",
				"items": {
					"oneOf": [
						{
							"type": "object",
							"properties": {
								"order": { "type": "integer" },
								"type": { "const": "wait" },
								"target": { "type": "string" },
								"namespace": { "type": "string" },
								"parameters": { "type": "object", "additionalProperties": { "type": "string" }, "default": {} },
								"rationale": { "type": "string" },
								"wait_seconds": { "type": "integer" }
							},
							"required": ["order", "type", "target", "namespace", "rationale"],
							"additionalProperties": false
						},
						{
							"type": "object",
							"properties": {
								"order": { "type": "integer" },
								"type": { "const": "patch_resources" },
								"target": { "type": "string" },
								"namespace": { "type": "string" },
								"parameters": {
									"type": "object",
									"properties": {
										"cpu_request": { "type": "string" },
										"cpu_limit": { "type": "string" },
										"memory_request": { "type": "string" },
										"memory_limit": { "type": "string" }
									},
									"minProperties": 1,
									"additionalProperties": false
								},
								"rationale": { "type": "string" },
								"wait_seconds": { "type": "integer" }
							},
							"required": ["order", "type", "target", "namespace", "parameters", "rationale"],
							"additionalProperties": false
						},
						{
							"type": "object",
							"properties": {
								"order": { "type": "integer" },
								"type": { "const": "scale_replicas" },
								"target": { "type": "string" },
								"namespace": { "type": "string" },
								"parameters": {
									"type": "object",
									"properties": {
										"replicas": { "type": "string" },
										"deployment": { "type": "string" }
									},
									"required": ["replicas"],
									"additionalProperties": false
								},
								"rationale": { "type": "string" },
								"wait_seconds": { "type": "integer" }
							},
							"required": ["order", "type", "target", "namespace", "parameters", "rationale"],
							"additionalProperties": false
						},
						{
							"type": "object",
							"properties": {
								"order": { "type": "integer", "description": "Step number starting at 1" },
								"type": {
									"type": "string",
									"enum": ["restart_pod", "rollback_deployment", "delete_pod", "escalate", "noop"],
									"description": "Step action type"
								},
								"target": { "type": "string" },
								"namespace": { "type": "string" },
								"parameters": { "type": "object", "additionalProperties": { "type": "string" }, "default": {} },
								"rationale": { "type": "string" },
								"wait_seconds": { "type": "integer", "description": "Pause after this step completes" }
							},
							"required": ["order", "type", "target", "namespace", "rationale"],
							"additionalProperties": false
						}
					]
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
