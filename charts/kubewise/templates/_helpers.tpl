{{- define "kubewise.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "kubewise.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s" $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "kubewise.namespace" -}}
{{- .Values.namespace.name }}
{{- end }}

{{- define "kubewise.labels" -}}
app.kubernetes.io/name: {{ include "kubewise.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: agent
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ include "kubewise.name" . }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "kubewise.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubewise.name" . }}
app.kubernetes.io/component: agent
{{- end }}

{{- define "kubewise.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "kubewise.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "kubewise.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "kubewise.fullname" . }}-secret
{{- end }}
{{- end }}

{{- define "kubewise.lokiURL" -}}
http://{{ include "kubewise.fullname" . }}-loki-gateway.{{ include "kubewise.namespace" . }}.svc.cluster.local
{{- end }}

{{- define "kubewise.tempoURL" -}}
http://{{ include "kubewise.fullname" . }}-tempo.{{ include "kubewise.namespace" . }}.svc.cluster.local:3200
{{- end }}

{{- define "kubewise.lokiPushURL" -}}
http://{{ include "kubewise.fullname" . }}-loki.{{ include "kubewise.namespace" . }}.svc.cluster.local:3100
{{- end }}

{{- define "kubewise.tempoOTLPEndpoint" -}}
http://{{ include "kubewise.fullname" . }}-tempo.{{ include "kubewise.namespace" . }}.svc.cluster.local:4318
{{- end }}

{{- define "kubewise.observabilityStack" -}}
{{- and .Values.agent.features.observability .Values.agent.observability.loki.enabled -}}
{{- end }}
