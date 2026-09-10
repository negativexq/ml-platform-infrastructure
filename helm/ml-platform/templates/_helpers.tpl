{{- define "ml-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ml-platform.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-inference" (include "ml-platform.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "ml-platform.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "ml-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/component: inference
app.kubernetes.io/part-of: ml-platform
app.kubernetes.io/managed-by: {{ .Release.Service }}
ml-platform.io/environment: {{ .Values.environment | quote }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "ml-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ml-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: inference
{{- end }}

{{- define "ml-platform.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "ml-platform.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding artifact-store credentials, if any. */}}
{{- define "ml-platform.artifactSecretName" -}}
{{- if eq .Values.artifactStore.mode "existing" }}
{{- required "artifactStore.existingSecret is required when mode is 'existing'" .Values.artifactStore.existingSecret }}
{{- else if eq .Values.artifactStore.mode "local" }}
{{- printf "%s-artifact-store" (include "ml-platform.fullname" .) }}
{{- end }}
{{- end }}
