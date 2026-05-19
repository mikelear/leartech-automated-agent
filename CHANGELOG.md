# Changelog

All notable changes to leartech-automated-agent are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Stub CHANGELOG.md (this file).

## [0.7.x] — 2026-05-18 / 2026-05-19

### Added

- Phase 0 dynamic MCP registry — `POST/DELETE/PUT /mcps` endpoints with
  GitOps PR-back persistence (PR #9 inadvertently included PR #6's content).
- Phase 0.75 `LlmConfig` per-role schema (PR #4).
- First self-modification initiative YAMLs (PRs #3, #5).
- Catalog coverage + golden-prompt snapshot tests (PR #1).
- Multi-cluster failure-mode regression tests (PR #2).
- Lighthouse `.lighthouse/jenkins-x/` pipeline files for ai-review +
  security-scan (PR #7) — closing the asymmetric-CI-bar gap.

### Fixed

- mypy strict cleanup across 5 files (PR #10).
- tekton-git ExternalSecret Vault property — `token` not `password` (PR #10).
- Secret replication labels for PR preview namespaces (PR #9).
- Calibration lessons promoted to status: encoded — iteration-summary +
  preflight-target-repo-quality-check (PR #10).
