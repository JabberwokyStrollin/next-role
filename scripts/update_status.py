"""
update_status.py — Log applications and update application status.

No Claude involvement. Pure JSON read/write.

Usage:
    # Log a new application (after submitting)
    python scripts/update_status.py log --job-id <uuid> --method greenhouse

    # Update status on an existing application
    python scripts/update_status.py status --app-id <uuid> --status recruiter_screen

    # List all applications
    python scripts/update_status.py list
"""

import argparse
import uuid as uuid_lib
import sys

from config import (
    APPLICATION_TRACKER_PATH,
    JOB_PIPELINE_PATH,
    COMPANY_REGISTRY_PATH,
    PROCESS_LOG_PATH,
    REJECTION_REASONS,
    composite_score,
    derive_country,
    load_json,
    save_json,
    now_utc,
    today,
    auto_age_application,
    find_duplicate_application,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def append_log(entry: dict) -> None:
    log = load_json(PROCESS_LOG_PATH)
    log.append({
        "log_id":       str(uuid_lib.uuid4()),
        "timestamp":    now_utc(),
        "session_date": today(),
        **entry,
    })
    save_json(PROCESS_LOG_PATH, log)


# `derive_country` (CA/IE/US/OTHER) is the canonical config SSOT — imported
# above. The application `country` field is derived at log time via that helper.


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_log(args):
    """Log a new application after submitting."""
    jobs = load_json(JOB_PIPELINE_PATH)
    job  = next((j for j in jobs if j["job_id"] == args.job_id), None)
    if not job:
        print(f"Error: job ID {args.job_id} not found in pipeline.")
        sys.exit(1)

    apps = load_json(APPLICATION_TRACKER_PATH)

    # Check for existing application on this exact job
    existing = next((a for a in apps if a.get("job_id") == args.job_id), None)
    if existing:
        print(f"Application already logged for this job: {existing['application_id']}")
        print(f"Use 'status' command to update it.")
        return

    # Guard against re-applying to effectively the same role under a different
    # listing (the StackAdapt repost case): same company + same core title.
    dupe = find_duplicate_application(job.get("company_id"), job.get("title", ""), apps)
    if dupe and not args.force:
        print(
            f"⚠ Possible duplicate — you already applied to "
            f"{dupe.get('company_name')} — {dupe.get('title')} "
            f"(status: {dupe.get('status')}, applied {dupe.get('date_applied')})."
        )
        print(
            "This looks like the same role under a different listing. "
            "Re-run with --force to log anyway."
        )
        sys.exit(2)

    # Derive country
    country = derive_country(job.get("location", ""))

    app = {
        "application_id":        str(uuid_lib.uuid4()),
        "job_id":                job["job_id"],
        "company_id":            job.get("company_id"),
        "company_name":          job["company_name"],
        "title":                 job["title"],
        "apply_url":             job["apply_url"],
        "location":              job.get("location", ""),
        "country":               country,
        "date_applied":          today(),
        "application_method":    args.method or "direct",
        "cover_letter_version":  job.get("cover_letter_version", 1),
        "plain_text_submitted":  args.plain_text,
        "composite_score_at_apply": None,  # populated below
        "status":                "applied",
        "status_updated":        now_utc(),
        "response_date":         None,
        "ghosted_flag":          False,
        "rejection_reason":      None,
        "notes":                 args.notes or "",
        "inaccuracies_noted":    "",
    }

    # Compute composite score snapshot
    companies = load_json(COMPANY_REGISTRY_PATH)
    company   = next((c for c in companies if c["company_id"] == job.get("company_id")), None)
    app["composite_score_at_apply"] = composite_score(job, company)

    apps.append(app)
    save_json(APPLICATION_TRACKER_PATH, apps)

    # Update pipeline status
    updated_jobs = [
        {**j, "pipeline_status": "applied"} if j["job_id"] == args.job_id else j
        for j in jobs
    ]
    save_json(JOB_PIPELINE_PATH, updated_jobs)

    append_log({
        "event_type":  "application_logged",
        "entity_type": "application",
        "entity_id":   app["application_id"],
        "entity_name": f"{app['company_name']} — {app['title']}",
        "detail": (
            f"Application logged. Method: {app['application_method']}. "
            f"Country: {country}. CL v{app['cover_letter_version']}. "
            f"Score at apply: {app['composite_score_at_apply']}."
        ),
    })

    print(f"Application logged: {app['application_id']}")
    print(f"  Company:   {app['company_name']}")
    print(f"  Title:     {app['title']}")
    print(f"  Date:      {app['date_applied']}")
    print(f"  Country:   {country}")
    print(f"  Score:     {app['composite_score_at_apply']}")


def cmd_status(args):
    """Update status on an existing application."""
    apps = load_json(APPLICATION_TRACKER_PATH)
    app  = next((a for a in apps if a["application_id"] == args.app_id), None)
    if not app:
        print(f"Error: application ID {args.app_id} not found.")
        sys.exit(1)

    old_status = app["status"]
    first_response = (
        not app.get("response_date") and
        args.status not in ["applied", "ghosted"]
    )

    app["status"]         = args.status
    app["status_updated"] = now_utc()
    if first_response:
        app["response_date"] = today()

    # Structured rejection reason (SSOT in config.REJECTION_REASONS). Only
    # meaningful when status == rejected; the human label is also appended to
    # notes so the free-text history still reads naturally.
    if args.rejection_reason:
        app["rejection_reason"] = args.rejection_reason
        label = REJECTION_REASONS.get(args.rejection_reason, args.rejection_reason)
        if label and label not in (app.get("notes") or ""):
            app["notes"] = ((app.get("notes") or "") + ("\n" if app.get("notes") else "") + label).strip()

    if args.notes:
        app["notes"] = (app.get("notes", "") + ("\n" if app.get("notes") else "") + args.notes).strip()

    updated = [app if a["application_id"] == args.app_id else a for a in apps]
    save_json(APPLICATION_TRACKER_PATH, updated)

    append_log({
        "event_type":  "application_status_change",
        "entity_type": "application",
        "entity_id":   app["application_id"],
        "entity_name": f"{app['company_name']} — {app['title']}",
        "detail":      f"Status: {old_status} → {args.status}.",
    })

    print(f"Status updated: {old_status} → {args.status}")
    if first_response:
        print(f"Response date set: {today()}")


def cmd_list(args):
    """List all applications with current status."""
    apps = load_json(APPLICATION_TRACKER_PATH)
    if not apps:
        print("No applications logged yet.")
        return

    # Run time-based aging (applied → ghosted → rejected). Shares the SSOT in
    # config.auto_age_application with serve.py:apply_ghosted_check.
    updated = False
    for app in apps:
        if auto_age_application(app):
            updated = True

    if updated:
        save_json(APPLICATION_TRACKER_PATH, apps)

    # Sort by date applied descending
    sorted_apps = sorted(apps, key=lambda a: a.get("date_applied", ""), reverse=True)

    print(f"\n{'Company':<22} {'Title':<35} {'Applied':<12} {'Status':<18} {'Score':<6} {'Ghost'}")
    print("─" * 105)
    for a in sorted_apps:
        ghost = "YES" if a.get("ghosted_flag") else ""
        print(
            f"{a['company_name']:<22} "
            f"{a['title'][:34]:<35} "
            f"{a.get('date_applied',''):<12} "
            f"{a['status']:<18} "
            f"{str(a.get('composite_score_at_apply') or ''):<6} "
            f"{ghost}"
        )
    print(f"\nTotal: {len(sorted_apps)} applications")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Track job applications.")
    sub    = parser.add_subparsers(dest="command", required=True)

    # log subcommand
    log_p = sub.add_parser("log", help="Log a new application")
    log_p.add_argument("--job-id",    required=True, metavar="UUID")
    log_p.add_argument("--method",    default="direct",
                       choices=["greenhouse","lever","workday","builtin",
                                "linkedin","direct","other"])
    log_p.add_argument("--plain-text", action="store_true",
                       help="Plain text version was submitted")
    log_p.add_argument("--notes",    metavar="TEXT", help="Optional notes")
    log_p.add_argument("--force",    action="store_true",
                       help="Log even if a duplicate application (same company "
                            "+ core title) already exists")

    # status subcommand
    st_p = sub.add_parser("status", help="Update application status")
    st_p.add_argument("--app-id",  required=True, metavar="UUID")
    st_p.add_argument("--status",  required=True,
                      choices=["applied","recruiter_screen","interview",
                               "offer","rejected","ghosted","withdrawn"])
    st_p.add_argument("--rejection-reason", dest="rejection_reason",
                      choices=list(REJECTION_REASONS),
                      help="Structured rejection reason (only with --status rejected)")
    st_p.add_argument("--notes",   metavar="TEXT", help="Optional notes")

    # list subcommand
    sub.add_parser("list", help="List all applications")

    args = parser.parse_args()

    if args.command == "log":
        cmd_log(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
