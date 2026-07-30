-- Platform Health Agent state schema.
-- Applied by db-init Job (PreSync hook) on first deploy.
-- Idempotent — uses CREATE … IF NOT EXISTS.

-- The investigations table is the single source of truth for the
-- agent's lifecycle: every event becomes a row, every status change
-- updates that row, the remediation result is appended in-place.
-- Replay/audit queries: SELECT * FROM investigations ORDER BY created_at DESC;
CREATE TABLE IF NOT EXISTS investigations (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    status             TEXT NOT NULL CHECK (status IN (
                          'pending',           -- row exists, Job not yet spawned
                          'running',           -- Investigator Job is active
                          'awaiting_approval', -- findings awaiting approval in the dashboard
                          'remediating',       -- approved, Remediator Job is active
                          'done',              -- remediation completed (success or failure)
                          'dismissed',         -- user clicked Dismiss
                          'expired',           -- older than APPROVAL_EXPIRY_HOURS, no action
                          'failed'             -- Investigator Job itself failed (e.g. kiro-cli error)
                       )),
    trigger_kind       TEXT NOT NULL,          -- 'CrashLoopBackOff', 'OOMKilled', etc.
    resource_kind      TEXT NOT NULL,          -- 'Pod' | 'Node' | …
    resource_namespace TEXT NOT NULL DEFAULT '',  -- '' for cluster-scoped resources (Nodes)
    resource_name      TEXT NOT NULL,
    event_payload      JSONB NOT NULL,         -- raw event/condition that triggered
    findings           JSONB,                  -- written by Investigator
    fix_commands       JSONB,                  -- extracted from findings.fix_commands
    out_of_scope       BOOLEAN NOT NULL DEFAULT false,
    approved_by        TEXT,                   -- user id of the approver (from the dashboard)
    approved_at        TIMESTAMPTZ,
    remediation_result JSONB,                  -- written by Remediator
    completed_at       TIMESTAMPTZ,
    error_message      TEXT                    -- populated on 'failed'
);
CREATE INDEX IF NOT EXISTS investigations_status_idx  ON investigations(status);
CREATE INDEX IF NOT EXISTS investigations_created_idx ON investigations(created_at DESC);

-- Per-resource debounce. The Event Watcher checks this BEFORE spawning
-- a Job: if last_seen is within DEBOUNCE_WINDOW_SEC, skip silently.
CREATE TABLE IF NOT EXISTS debounce (
    resource_kind      TEXT NOT NULL,
    resource_namespace TEXT NOT NULL DEFAULT '',
    resource_name      TEXT NOT NULL,
    last_seen          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (resource_kind, resource_namespace, resource_name)
);

-- Daily cost / concurrency counters.
-- One row per day. Incremented by Event Watcher (investigations) and
-- the cluster-dashboard backend (remediations). Both check the
-- counter before acting; if exceeded, action is suppressed.
CREATE TABLE IF NOT EXISTS daily_counters (
    day            DATE PRIMARY KEY,
    investigations INTEGER NOT NULL DEFAULT 0,
    remediations   INTEGER NOT NULL DEFAULT 0
);

-- Helper view: today's counters with sane defaults.
CREATE OR REPLACE VIEW today_counters AS
SELECT
    CURRENT_DATE                                        AS day,
    COALESCE(c.investigations, 0)                       AS investigations,
    COALESCE(c.remediations, 0)                         AS remediations
FROM (SELECT 1) AS dummy
LEFT JOIN daily_counters c ON c.day = CURRENT_DATE;

-- Helper view: count of active (running/awaiting_approval/remediating)
-- investigations. Used for concurrency cap.
CREATE OR REPLACE VIEW active_investigation_count AS
SELECT COUNT(*) AS n
FROM investigations
WHERE status IN ('running', 'awaiting_approval', 'remediating');
