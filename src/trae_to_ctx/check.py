"""Structural checks for a ctx-history-jsonl-v2 file written by `trae-to-ctx export --jsonl`.

Usage: trae-to-ctx check <export.jsonl>   (reads <export.jsonl>.report.json when present)

Checks the closed record envelopes, identities, ordering and references, and that every Trae
message counted in the report is either an event or a named skip. Prints counts only, no content.
"""
import json
import os
import sys

FIELDS = {
    "manifest": ({"schema_version"}, {"lineage_contract", "producer", "exported_at", "metadata"}),
    "source": ({"source_id", "provider_key", "source_format"},
               {"raw_uri", "raw_source_path", "fingerprint", "importer_version", "observed_at", "machine_id",
                "trust", "fidelity", "cursor", "metadata"}),
    "session": ({"source_id", "provider_session_id", "started_at"},
                {"parent_provider_session_id", "root_provider_session_id", "session_relationship",
                 "external_agent_id", "cwd", "ended_at", "agent_scope", "role_hint", "status", "fidelity",
                 "idempotency_key", "artifacts", "metadata"}),
    "event": ({"source_id", "provider_session_id", "event_index", "occurred_at"},
              {"event_id", "copied_from", "native_cursor", "event_hash", "event_type", "role", "fidelity",
               "idempotency_key", "artifacts", "payload", "preview", "metadata"}),
    "file_reference": ({"source_id", "provider_session_id", "reference_index", "event_index", "value", "occurred_at"},
                       {"metadata"}),
    "edge": ({"source_id", "from_provider_session_id", "to_provider_session_id"},
             {"edge_id", "relationship", "occurred_at", "fidelity", "metadata"}),
}
EVENT_TYPES = {"message", "tool_call", "tool_output", "command_started", "command_output", "command_finished",
               "artifact", "summary", "notice"}
MAX_RECORD = 16 * 1024 * 1024


def main(path):
    errors, counts = [], {}
    sessions, events, refs = {}, {}, set()
    sources, manifests = set(), 0
    largest = 0
    msg_ids = {"user": set(), "assistant": set(), "tool": set(), "inline user": set(), "inline assistant": set()}

    def err(n, msg):
        if len(errors) < 50:
            errors.append(f"line {n}: {msg}")

    with open(path, "rb") as f:
        for n, raw in enumerate(f, 1):
            largest = max(largest, len(raw))
            if len(raw) > MAX_RECORD:
                err(n, "record over 16 MiB")
            r = json.loads(raw)
            rt = r.get("record_type")
            counts[rt] = counts.get(rt, 0) + 1
            if rt not in FIELDS:
                err(n, f"unknown record_type {rt!r}")
                continue
            req, opt = FIELDS[rt]
            keys = set(r) - {"record_type"}
            if req - keys:
                err(n, f"{rt} missing {sorted(req - keys)}")
            if keys - req - opt:
                err(n, f"{rt} unknown fields {sorted(keys - req - opt)}")
            if rt == "manifest":
                manifests += 1
                if n != 1:
                    err(n, "manifest is not the first line")
            elif rt == "source":
                sources.add(r["source_id"])
            elif rt in ("session", "event", "file_reference", "edge") and r["source_id"] not in sources:
                err(n, f"{rt} before its source")
            if rt == "session":
                sid = r["provider_session_id"]
                if sid in sessions:
                    err(n, f"duplicate session {sid}")
                p = r.get("parent_provider_session_id")
                if p and p not in sessions:
                    err(n, "child session before its parent")
                if (r.get("session_relationship") == "root") == bool(p):
                    err(n, "root/parent mismatch")
                sessions[sid] = {"next": 0, "ids": set()}
            elif rt == "event":
                s = sessions.get(r["provider_session_id"])
                if s is None:
                    err(n, "event before its session")
                    continue
                if r["event_index"] != s["next"]:
                    err(n, f"event_index {r['event_index']} expected {s['next']}")
                s["next"] = r["event_index"] + 1
                eid = r.get("event_id")
                if not eid or eid in s["ids"]:
                    err(n, f"missing or duplicate event_id {eid!r}")
                s["ids"].add(eid)
                if r.get("event_type") not in EVENT_TYPES:
                    err(n, f"bad event_type {r.get('event_type')!r}")
                events[r["event_type"]] = events.get(r["event_type"], 0) + 1
                # map back to the Trae message: "<history_v2_id>:<i>[:call:<k>]" or "cm:<message_id>"
                parts = eid.split(":")
                role = r.get("role")
                if parts[0] == "cm":
                    msg_ids[f"inline {role}"].add(eid)
                elif len(parts) >= 2 and parts[1].isdigit() and role in msg_ids:
                    msg_ids[role].add((parts[0], parts[1]))
            elif rt == "file_reference":
                s = sessions.get(r["provider_session_id"])
                if s is None or not (0 <= r["event_index"] < s["next"]):
                    err(n, "file_reference to an event not yet written")
                key = (r["provider_session_id"], r["reference_index"])
                if key in refs:
                    err(n, "duplicate reference_index")
                refs.add(key)
                if not r["value"]:
                    err(n, "empty file value")
            elif rt == "edge":
                if r["from_provider_session_id"] not in sessions or r["to_provider_session_id"] not in sessions:
                    err(n, "edge to an unknown session")
    if manifests != 1:
        errors.append(f"{manifests} manifest records")

    out = {"records": counts, "events": events, "sessions": len(sessions), "largest_record_bytes": largest}
    rep_path = path + ".report.json"
    if os.path.exists(rep_path):
        with open(rep_path, encoding="utf-8") as f:
            rep = json.load(f)
        sk = rep.get("skipped", {})
        skipped_by_role = {
            "user": sk.get("user message with no text or image", 0),
            "assistant": sk.get("assistant message with no text or tool calls", 0),
        }
        acct = {}
        for role, total in rep.get("messages", {}).items():
            exported = len(msg_ids.get(role, ()))
            skipped = skipped_by_role.get(role.replace("inline ", ""), 0) if role in ("user", "assistant") else 0
            acct[role] = {"in_trae": total, "exported": exported, "skipped": skipped}
        # inline skips share the reason names with history skips; check the totals together
        for base in ("user", "assistant"):
            t = sum(acct.get(r, {}).get("in_trae", 0) for r in (base, f"inline {base}"))
            e = sum(acct.get(r, {}).get("exported", 0) for r in (base, f"inline {base}"))
            if t != e + skipped_by_role[base]:
                errors.append(f"{base}: {t} in Trae != {e} exported + {skipped_by_role[base]} skipped")
        if acct.get("tool", {}).get("in_trae", 0) != acct.get("tool", {}).get("exported", 0):
            errors.append("tool results: count mismatch")
        out["accounting"] = acct
        if rep.get("events") != events:
            errors.append(f"report events {rep.get('events')} != file events {events}")
    out["errors"] = errors
    print(json.dumps(out, indent=2, sort_keys=True))
    return 1 if errors else 0

