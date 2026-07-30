#!/usr/bin/env python3
"""Persist Investigator/Remediator findings to the postgres state DB.

Replaces the former post_to_slack.py. The dashboard reads the same
table and renders the approvals UI; this script has no external
dependencies beyond psycopg.

Usage:
    persist_findings.py investigation <investigation_id> <findings.json>
    persist_findings.py remediation   <investigation_id> <result.json>
    persist_findings.py error         <investigation_id> <message>
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import psycopg


def db_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        autocommit=True,
        connect_timeout=10,
    )


def persist_investigation(investigation_id: str, findings_path: str) -> int:
    with open(findings_path) as f:
        findings = json.load(f)

    out_of_scope = bool(findings.get("out_of_scope", False))
    fix_commands = findings.get("fix_commands") or []

    # Run the deterministic allowlist over the proposed commands so the operator
    # sees, at approval time, whether the plan is within policy. Violations are
    # recorded on the finding and force manual review; the remediator re-checks
    # and hard-refuses (fix_command_policy.py), so this is advisory-for-display,
    # not the enforcement point. Import is best-effort (same /scripts dir).
    try:
        import fix_command_policy
        policy_violations = fix_command_policy.validate(fix_commands)
    except Exception as e:  # never fail persistence because the checker errored
        policy_violations = [f"policy check unavailable: {type(e).__name__}"]
    if policy_violations:
        findings["policy_violations"] = policy_violations
        findings["requires_manual_review"] = True

    new_status = "dismissed" if out_of_scope else "awaiting_approval"

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE investigations
                      SET findings = %s,
                          fix_commands = %s,
                          out_of_scope = %s,
                          status = %s,
                          completed_at = CASE WHEN %s THEN now() ELSE NULL END
                    WHERE id = %s""",
                (json.dumps(findings),
                 json.dumps(fix_commands),
                 out_of_scope,
                 new_status,
                 out_of_scope,                        # out-of-scope completes immediately
                 investigation_id),
            )
            if cur.rowcount == 0:
                print(f"investigation {investigation_id} not found", file=sys.stderr)
                return 1
    print(f"persisted investigation {investigation_id}: status={new_status} out_of_scope={out_of_scope}")
    return 0


def persist_remediation(investigation_id: str, result_path: str) -> int:
    with open(result_path) as f:
        result = json.load(f)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE investigations
                      SET remediation_result = %s,
                          status = 'done',
                          completed_at = now()
                    WHERE id = %s""",
                (json.dumps(result), investigation_id),
            )
            if cur.rowcount == 0:
                print(f"investigation {investigation_id} not found", file=sys.stderr)
                return 1
    print(f"persisted remediation result for {investigation_id}")
    return 0


def persist_error(investigation_id: str, message: str) -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE investigations
                      SET status='failed',
                          error_message=%s,
                          completed_at=now()
                    WHERE id=%s""",
                (message, investigation_id),
            )
            if cur.rowcount == 0:
                print(f"investigation {investigation_id} not found", file=sys.stderr)
                return 1
    print(f"persisted error for {investigation_id}: {message[:80]}")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: persist_findings.py {investigation|remediation|error} <investigation_id> [path|message]",
              file=sys.stderr)
        return 2

    mode = sys.argv[1]
    investigation_id = sys.argv[2]

    if mode == "investigation":
        return persist_investigation(investigation_id, sys.argv[3])
    if mode == "remediation":
        return persist_remediation(investigation_id, sys.argv[3])
    if mode == "error":
        return persist_error(investigation_id, sys.argv[3])

    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
