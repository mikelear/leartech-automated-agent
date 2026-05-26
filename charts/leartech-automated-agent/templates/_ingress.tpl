{{/*
Ingress template using jxRequirements pattern.

Domain and namespaceSubDomain come from jx-values.yaml (auto-generated
per cluster and per environment). No hardcoded domains anywhere.

Preview: jx preview create populates jx-values.yaml with the cluster's
domain + namespaceSubDomain "-prN."
Staging: jx-values.yaml in the GitOps helmfile has domain + "-jx-staging."
Production: same pattern with "-jx-production."

Usage in chart templates/ingress.yaml:
  {{ include "leartech.ingress" . }}

Requires values:
  jxRequirements.ingress.domain
  jxRequirements.ingress.namespaceSubDomain (default: -jx.)
  service.name (or falls back to fullname)
  service.externalPort (default: 8080)
  ingress.annotations (optional)
  ingress.pathType (default: ImplementationSpecific)
  ingress.labels (optional)
*/}}

{{- define "leartech.ingress" -}}
{{- if and (.Values.jxRequirements.ingress.domain) (not .Values.knativeDeploy) }}
{{- $hostName := .Values.service.name | default (include "leartech.fullname" .) }}
{{- $backendName := include "leartech.fullname" . }}
{{- $svcPort := .Values.service.externalPort | default 8080 }}
{{- $annotations := dict }}
{{- $_ := merge $annotations (.Values.ingress.annotations | default dict) (.Values.jxRequirements.ingress.annotations | default dict) }}
{{- if not (hasKey $annotations "kubernetes.io/ingress.class") }}
{{- $_ := set $annotations "kubernetes.io/ingress.class" (.Values.ingress.classAnnotation | default "nginx") }}
{{- end }}
apiVersion: {{ .Values.jxRequirements.ingress.apiVersion | default "networking.k8s.io/v1" }}
kind: Ingress
metadata:
  name: {{ $hostName }}
  labels:
    {{- include "leartech.labels" . | nindent 4 }}
    {{- with .Values.ingress.labels }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  {{- if $annotations }}
  annotations:
    {{- toYaml $annotations | nindent 4 }}
  {{- end }}
spec:
  rules:
  - host: {{ $hostName }}{{ .Values.jxRequirements.ingress.namespaceSubDomain }}{{ .Values.jxRequirements.ingress.domain }}
    http:
      paths:
      - path: /
        pathType: {{ .Values.ingress.pathType | default "ImplementationSpecific" }}
        backend:
          service:
            name: {{ $backendName }}
            port:
              number: {{ $svcPort }}
{{- if .Values.jxRequirements.ingress.tls.enabled }}
  tls:
  - hosts:
    - {{ $hostName }}{{ .Values.jxRequirements.ingress.namespaceSubDomain }}{{ .Values.jxRequirements.ingress.domain }}
{{- if .Values.jxRequirements.ingress.tls.production }}
    secretName: "tls-{{ .Values.jxRequirements.ingress.domain | replace "." "-" }}-p"
{{- else }}
    secretName: "tls-{{ .Values.jxRequirements.ingress.domain | replace "." "-" }}-s"
{{- end }}
{{- end }}
{{- end }}
{{- end -}}
