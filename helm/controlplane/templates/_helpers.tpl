{{- define "controlplane.name" -}}
{{- printf "%s-controlplane" .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- define "controlplane.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end -}}
{{- define "controlplane.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}
{{- define "controlplane.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}
{{- define "controlplane.databaseEnv" -}}
{{- $database := index .root.Values.database .component -}}
- name: CP_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $database.existingSecret | quote }}
      key: {{ $database.urlKey | quote }}
{{- end -}}
