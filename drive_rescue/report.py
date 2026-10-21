"""A plain-text report of what happened, to keep or to hand to a technician or an insurer."""
import time

from .util import human_size


def _drive_lines(label, drive):
    if not drive:
        return []
    out = [f"{label}: {drive.get('name') or 'Unknown drive'} ({drive.get('size_text') or human_size(drive.get('size', 0))})"]
    if drive.get("serial"):
        out.append(f"  Serial number: {drive['serial']}")
    if drive.get("kind"):
        out.append(f"  Type: {drive['kind']}{' · ' + drive['transport'] if drive.get('transport') else ''}")
    return out


def _health_lines(health):
    if not health:
        return []
    out = [f"Health check: {health.get('label', 'Unknown')}"]
    for d in health.get("details") or []:
        out.append(f"  {d['k']}: {d['v']}")
    return out


def build_report(plan, snap, health=None, task=None, when=None):
    """Return the report as text. `plan` and `snap` are the job's plan and snapshot."""
    clone = (snap or {}).get("mode") == "clone"
    when = when or time.strftime("%A %d %B %Y, %H:%M")
    lines = ["DRIVE RESCUE REPORT", "=" * 19, f"Date: {when}",
             f"Job: {'Clone a drive' if clone else 'Rescue files from a failing drive'}", ""]
    lines += _drive_lines("Original drive" if clone else "Failing drive", plan.get("source"))
    lines += _drive_lines("Target drive" if clone else "Saved to", plan.get("dest_drive"))
    if not clone and plan.get("target_dir"):
        lines.append(f"Folder: {plan['target_dir']}")
        if plan.get("image"):
            lines.append(f"Image file: {plan['image']}")
    lines.append("")
    lines += _health_lines(health)
    lines.append("")
    if snap and snap.get("state") != "none":
        lines.append("THE COPY")
        lines.append(f"  Result: {snap.get('percent', 0)}% copied ({snap.get('finished_text', '')} of {snap.get('size_text', '')})")
        lines.append(f"  {'Not readable' if clone else 'Could not be read'}: {snap.get('skipped_text', '0 B')}")
        if snap.get("elapsed_text") and snap["elapsed_text"] != "-":
            lines.append(f"  Time taken: {snap['elapsed_text']}")
        lines.append(f"  Finished completely: {'yes' if snap.get('complete') else 'no'}")
        o = snap.get("outlook")
        if o:
            lines.append(f"  Outlook: {o['title']}. {o['text']}")
            if o.get("bad_areas"):
                lines.append(f"  Unreadable areas: {o['bad_areas']} (largest {human_size(o['largest_bad'])})")
        res = snap.get("result") or {}
        for l in res.get("lines") or []:
            lines.append(f"  {l}")
        for w in res.get("warnings") or []:
            lines.append(f"  Note: {w}")
        lines.append("")
    if task and task.get("state") not in (None, "idle"):
        what = "files found by scanning" if task.get("kind") == "carve" else "files recovered"
        lines.append("FILES")
        lines.append(f"  {task.get('copied', 0)} {what}" + (f" ({task['bytes_text']})" if task.get("bytes") else ""))
        if task.get("failed_count"):
            lines.append(f"  {task['failed_count']} could not be read:")
            for f in (task.get("failed") or [])[:20]:
                lines.append(f"    - {f['path']} ({f['reason']})")
            if task["failed_count"] > 20:
                lines.append("    - and more")
        if task.get("out_dir"):
            lines.append(f"  Saved in: {task['out_dir']}")
        lines.append("")
    lines.append("The original drive was only ever read from, never written to." if not clone else
                 "The original drive was only ever read from. Everything on the target drive was replaced.")
    return "\n".join(lines) + "\n"
