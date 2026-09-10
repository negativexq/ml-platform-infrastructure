{{- define "platform-local.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: ml-platform
app.kubernetes.io/managed-by: {{ .Release.Service }}
ml-platform.io/tier: platform
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* Postgres connection string used by MLflow. */}}
{{- define "platform-local.postgresUri" -}}
postgresql://{{ .Values.postgres.auth.username }}:{{ .Values.postgres.auth.password }}@platform-postgres:5432/{{ .Values.postgres.auth.database }}
{{- end }}
