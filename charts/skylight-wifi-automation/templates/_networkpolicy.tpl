{{- define "skylight-wifi-automation.uiPeers" -}}
{{- range .Values.networkPolicy.ui.cidrs }}
- ipBlock:
    cidr: {{ . | quote }}
{{- end }}
{{- range .Values.networkPolicy.probeCidrs }}
- ipBlock:
    cidr: {{ . | quote }}
{{- end }}
{{- range .Values.networkPolicy.ui.namespaceSelectors }}
- namespaceSelector:
    {{- toYaml . | nindent 4 }}
{{- end }}
{{- range .Values.networkPolicy.ui.podSelectors }}
- podSelector:
    {{- toYaml . | nindent 4 }}
{{- end }}
{{- if .Values.networkPolicy.allowHelmTest }}
- podSelector:
    matchLabels:
      app.kubernetes.io/name: {{ include "skylight-wifi-automation.name" . }}
      app.kubernetes.io/instance: {{ .Release.Name }}
      app.kubernetes.io/component: test
{{- end }}
{{- end }}

{{- define "skylight-wifi-automation.metricPeers" -}}
{{- range .Values.networkPolicy.metrics.cidrs }}
- ipBlock:
    cidr: {{ . | quote }}
{{- end }}
{{- range .Values.networkPolicy.metrics.namespaceSelectors }}
- namespaceSelector:
    {{- toYaml . | nindent 4 }}
{{- end }}
{{- range .Values.networkPolicy.metrics.podSelectors }}
- podSelector:
    {{- toYaml . | nindent 4 }}
{{- end }}
{{- end }}
