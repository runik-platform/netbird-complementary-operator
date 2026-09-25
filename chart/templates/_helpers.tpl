{{/*
Expand the name of the chart.
*/}}
{{- define "netbird-complementary-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name. Truncated at 63 characters because some
Kubernetes name fields are limited to this (by the DNS naming spec).
*/}}
{{- define "netbird-complementary-operator.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "netbird-complementary-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "netbird-complementary-operator.labels" -}}
helm.sh/chart: {{ include "netbird-complementary-operator.chart" . }}
{{ include "netbird-complementary-operator.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: netbird
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "netbird-complementary-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "netbird-complementary-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "netbird-complementary-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "netbird-complementary-operator.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Service FQDN (used by CompositeController webhook URLs).
*/}}
{{- define "netbird-complementary-operator.serviceFQDN" -}}
{{- printf "%s.%s.svc.cluster.local" (include "netbird-complementary-operator.fullname" .) .Release.Namespace }}
{{- end }}

{{/*
Image reference.
*/}}
{{- define "netbird-complementary-operator.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end }}
