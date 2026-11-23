{{/*
Expand the chart name.
*/}}
{{- define "skylight-wifi-automation.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Expand the release name plus chart name.
*/}}
{{- define "skylight-wifi-automation.fullname" -}}
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

{{- define "skylight-wifi-automation.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "skylight-wifi-automation.selectorLabels" -}}
app.kubernetes.io/name: {{ include "skylight-wifi-automation.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "skylight-wifi-automation.labels" -}}
helm.sh/chart: {{ include "skylight-wifi-automation.chart" . }}
{{ include "skylight-wifi-automation.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "skylight-wifi-automation.name" . }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "skylight-wifi-automation.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.configMapName" -}}
{{- printf "%s-config" (include "skylight-wifi-automation.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "skylight-wifi-automation.dataClaimName" -}}
{{- if .Values.persistence.existingClaim }}
{{- .Values.persistence.existingClaim }}
{{- else }}
{{- printf "%s-data" (include "skylight-wifi-automation.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.backupClaimName" -}}
{{- if .Values.backup.persistence.existingClaim }}
{{- .Values.backup.persistence.existingClaim }}
{{- else }}
{{- printf "%s-backups" (include "skylight-wifi-automation.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else if or .Values.secrets.encryptionKey .Values.secrets.encryptionKeyNew .Values.secrets.setupToken .Values.secrets.skylightEmail .Values.secrets.skylightPassword .Values.secrets.unifiApiKey }}
{{- include "skylight-wifi-automation.fullname" . }}
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.createsSecret" -}}
{{- if and (not .Values.secrets.existingSecret) (include "skylight-wifi-automation.secretName" .) }}true{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.latchPath" -}}
{{- if .Values.env.latchPath }}
{{- .Values.env.latchPath }}
{{- else }}
{{- printf "%s/latch.json" (trimSuffix "/" .Values.env.dataDir) }}
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.configFile" -}}
{{- base .Values.env.configPath }}
{{- end }}

{{- define "skylight-wifi-automation.configDir" -}}
{{- dir .Values.env.configPath }}
{{- end }}

{{/*
YAML document imported once when CONFIG_SOURCE=db and the database has no config.
*/}}
{{- define "skylight-wifi-automation.configYaml" -}}
{{- $cfg := dict
      "timezone" .Values.config.timezone
      "poll_seconds" (.Values.config.poll_seconds | int)
      "dry_run" .Values.config.dry_run
      "include_late" .Values.config.include_late
      "include_routines" .Values.config.include_routines
      "empty_day_unlocks" .Values.config.empty_day_unlocks
      "unifi_url" .Values.config.unifi_url
      "unifi_tls_ca" .Values.config.unifi_tls_ca
      "children" .Values.config.children
-}}
{{- if .Values.config.unifi_site }}
{{- $_ := set $cfg "unifi_site" .Values.config.unifi_site }}
{{- end }}
{{- toYaml $cfg }}
{{- end }}

{{- define "skylight-wifi-automation.probe" -}}
httpGet:
  path: /healthz
  port: http
  scheme: HTTP
{{- if .initialDelaySeconds }}
initialDelaySeconds: {{ .initialDelaySeconds }}
{{- end }}
periodSeconds: {{ .periodSeconds }}
timeoutSeconds: {{ .timeoutSeconds }}
failureThreshold: {{ .failureThreshold }}
successThreshold: {{ .successThreshold }}
{{- end }}

{{- define "skylight-wifi-automation.podAnnotations" -}}
checksum/config: {{ include "skylight-wifi-automation.configYaml" . | sha256sum | quote }}
{{- if include "skylight-wifi-automation.createsSecret" . }}
checksum/secret: {{ include (print $.Template.BasePath "/secret.yaml") . | sha256sum | quote }}
{{- end }}
{{- if .Values.secrets.rolloutToken }}
checksum/secret-token: {{ .Values.secrets.rolloutToken | quote }}
{{- end }}
{{- with .Values.podAnnotations }}
{{ toYaml . }}
{{- end }}
{{- end }}
