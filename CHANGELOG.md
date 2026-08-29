# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `byp sync` — mirror a YouTube playlist into local SQLite (`playlistItems.list`
  + `videos.list`), with left-join video enrichment and soft-deletion of items
  that disappear between syncs
- `byp query` — run arbitrary DuckDB SQL over the local mirror, `--format
  table|csv|json`
- `byp order-from-query` — store a target playlist order from a SQL result,
  validated as a permutation of the synced items
- `byp reorder` — apply the target order via `playlistItems.update`, bounded by a
  daily quota budget (`--budget`, `--status`, `--dry-run`); LIS-based planner
  keeps moves to a minimum and the run resumes across days
- `byp import-watch-later` — read YouTube Watch Later via yt-dlp (blocked from the
  Data API) into the local DB (`--local`, zero quota) or into a real YouTube
  playlist (`--api`, 50 units per insert; resumable across runs)
- `byp import-wl-to-yt` — daily incremental job that imports remaining Watch Later
  videos into a target playlist, respecting the daily quota, idempotently
- systemd timer/service for automated daily Watch Later import at the PT quota reset
- OAuth 2.0 installed-app flow with automatic re-consent on refresh-token expiry
- Quota accounting table, measured against the midnight-Pacific reset
- Initial project structure with src layout
- GitHub Actions CI (lint, typecheck, security, tests with coverage)
- Pre-commit hooks (ruff, pyupgrade)
- Dependabot for automated dependency updates
- pyright type checking (strict on `src/`, standard on tests)
- pip-audit for vulnerability scanning

## [0.1.0] - 2026-08-28

### Added
- Project bootstrap with dev-standards baseline
- Basic CLI entry point
- Initial test suite
