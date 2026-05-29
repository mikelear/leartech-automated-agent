{{/*
Named template defining the Job spec used by initiative runs (Phase D.3+).

NOT rendered as a manifest by Helm. job_runner.py reads this template at
spawn time, interpolates per-initiative values, and POSTs the resulting
Job manifest to batch/v1.

Why a Helm-named template instead of a Python f-string:
  - Centralises the Job shape (RBAC subject, labels, resource policy) in
    the chart, alongside the SA + Role that grant it permissions. One file
    to read when reasoning about cluster-side runtime behaviour.
  - Lets values.yaml's `jobs.resources` provide defaults that GitOps can
    tune per-cluster without a code change.
  - Keeps job_runner.py small: it pulls the template, fills the dict,
    submits — no manifest construction logic in Python.

Inputs (passed as the dict argument when invoking via `tpl`):
  .runId              — unique run identifier; becomes Job name + label
  .namespace          — target namespace
  .initiative         — initiative slug; label for filtering / queries
  .serviceAccountName — SA the Job pod runs as (typically <fullname>-job-runner)
  .image              — agent image (repo:tag) — D.3 wires this from values
  .env                — map[string]string of plain env vars
  .secretRefs         — map[string]{secret,key} of valueFrom.secretKeyRef
  .resources          — map of requests/limits (cpu/memory); falls back to
                        the defaults below if unset
*/}}
{{- define "leartech-automated-agent.job-spec" -}}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .runId }}
  namespace: {{ .namespace }}
  labels:
    leartech.io/initiative: {{ .initiative }}
    leartech.io/run-id: {{ .runId }}
    leartech.io/component: initiative-runner
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: {{ .ttlSecondsAfterFinished | default 86400 }}
  template:
    metadata:
      labels:
        leartech.io/initiative: {{ .initiative }}
        leartech.io/run-id: {{ .runId }}
        leartech.io/component: initiative-runner
    spec:
      serviceAccountName: {{ .serviceAccountName }}
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
      containers:
      - name: agent
        image: {{ .image }}
        imagePullPolicy: Always
        # sh -c bootstrap writes the inlined YAML to /tmp/initiative.yaml
        # then execs the agent CLI with the positional INITIATIVE_PATH it
        # expects (`gate.agent.initiative` is a click.argument, not options).
        # The YAML body comes in via LEARTECH_INITIATIVE_YAML env var which
        # job_runner.py appends to .env at spawn time.
        command: ["sh", "-c"]
        args:
        - 'printf "%s" "$LEARTECH_INITIATIVE_YAML" > /tmp/initiative.yaml && exec python -m gate.agent.initiative /tmp/initiative.yaml'
        securityContext:
          runAsNonRoot: true
          runAsUser: 1000
          allowPrivilegeEscalation: false
          capabilities:
            drop:
            - ALL
        env:
        {{- range $name, $val := .env }}
        - name: {{ $name }}
          value: {{ $val | quote }}
        {{- end }}
        {{- range $name, $ref := .secretRefs }}
        - name: {{ $name }}
          valueFrom:
            secretKeyRef:
              name: {{ $ref.secret }}
              key: {{ $ref.key }}
        {{- end }}
        resources:
          requests:
            cpu: {{ .resources.requests.cpu | default "500m" }}
            memory: {{ .resources.requests.memory | default "1Gi" }}
          limits:
            cpu: {{ .resources.limits.cpu | default "4" }}
            memory: {{ .resources.limits.memory | default "8Gi" }}
        # Writable workspace at /workspace — agent loop clones consumer
        # repos here. The image's /workspace path is not writable by UID
        # 1000; emptyDir gives a per-Job scratch space with fsGroup=1000.
        volumeMounts:
        - name: workspace
          mountPath: /workspace
      volumes:
      - name: workspace
        emptyDir: {}
{{- end -}}
