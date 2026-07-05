package llm

import "encoding/json"

// RemediationSchema returns the JSON Schema for the remediation_plan structured output.
// This schema is used with OpenRouter's response_format: json_schema with strict: true.
func RemediationSchema() json.RawMessage {
	// JSON Schema as a raw message to avoid struct marshaling issues with nested schemas.
	// The strict mode requires additionalProperties: false at every level and all properties required.
	schema := `{
		"type": "object",
		"properties": {
			"diagnosis": {
				"type": "object",
				"properties": {
					"root_cause": { "type": "string", "description": "Root cause of the anomaly" },
					"severity": { "type": "string", "enum": ["critical", "warning", "info"], "description": "Severity level" },
					"confidence": { "type": "number", "description": "Confidence in the diagnosis, 0.0 to 1.0" },
					"evidence": { "type": "array", "items": { "type": "string" }, "description": "Specific evidence supporting the diagnosis" }
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
						"description": "The remediation action type"
					},
					"target": { "type": "string", "description": "Name of the target resource" },
					"namespace": { "type": "string", "description": "Kubernetes namespace of the target" },
					"parameters": { "type": "object", "additionalProperties": { "type": "string" }, "description": "Optional action parameters (e.g. replicas, cpu, memory)" },
					"rationale": { "type": "string", "description": "Why this action addresses the root cause" }
				},
				"required": ["type", "target", "namespace", "rationale"],
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
		"required": ["diagnosis", "action", "risk"],
		"additionalProperties": false
	}`
	return json.RawMessage(schema)
}
