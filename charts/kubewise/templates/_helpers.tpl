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

{{- define "kubewise.metricsURL" -}}
{{- $ns := .Release.Namespace -}}
{{- $url := or .Values.agent.observability.metricsURL .Values.agent.prometheusAddress -}}
{{- default (printf "http://%s-victoria-metrics-single-server.%s.svc.cluster.local:8428" .Release.Name $ns) $url }}
{{- end }}

{{- define "kubewise.lokiURL" -}}
{{- $ns := .Release.Namespace -}}
{{- default (printf "http://%s-victoria-logs-single-server.%s.svc.cluster.local:9428/select/loki" .Release.Name $ns) .Values.agent.observability.logsEndpoint }}
{{- end }}

{{- define "kubewise.tempoURL" -}}
{{- $ns := .Release.Namespace -}}
{{- default (printf "http://%s-tempo.%s.svc.cluster.local:3200" .Release.Name $ns) .Values.agent.observability.tracesEndpoint }}
{{- end }}

{{- define "kubewise.lokiPushURL" -}}
{{- $ns := .Release.Namespace -}}
{{- default (printf "http://%s-victoria-logs-single-server.%s.svc.cluster.local:9428" .Release.Name $ns) .Values.agent.observability.logsPushEndpoint }}
{{- end }}

{{- define "kubewise.tempoOTLPEndpoint" -}}
{{- $ns := .Release.Namespace -}}
{{- default (printf "http://%s-tempo.%s.svc.cluster.local:4318" .Release.Name $ns) .Values.agent.observability.tracesOTLPEndpoint }}
{{- end }}

{{- define "kubewise.observabilityStack" -}}
{{- if .Values.agent.features.observability }}true{{- end }}
{{- end }}
