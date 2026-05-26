{{/*
Expand the name of the chart.
*/}}
{{- define "leartech-automated-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "leartech-automated-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "leartech-automated-agent.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "leartech-automated-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "leartech-automated-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "leartech-automated-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Aliases required by the inlined leartech-helm-library helpers (_ingress.tpl).
These delegate to the existing leartech-automated-agent.* definitions above
so that library code calling leartech.fullname / leartech.labels works
without duplicating logic.
*/}}
{{- define "leartech.fullname" -}}
{{- include "leartech-automated-agent.fullname" . }}
{{- end }}

{{- define "leartech.labels" -}}
{{- include "leartech-automated-agent.labels" . }}
{{- end }}
