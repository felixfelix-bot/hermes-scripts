#!/usr/bin/env python3
"""ngit_ci_evidence.py — read-only fetcher for ngit (Nostr) CI evidence.

Reads kind-9842 workflow-result events from Nostr relays and reports the CI
conclusions for one repository, optionally narrowed to a commit and/or a ref.

This tool is READ-ONLY: it never publishes an event, never pushes a ref and
never triggers a run. The only network operation is `nak req` against the
given relays.

Usage
-----
    ngit_ci_evidence.py REPO_IDENTIFIER [--commit SHA] [--ref REF]
                        [--relays URL ...] [--limit N] [--json]
                        [--require-conclusion] [--timeout SECONDS]

Event shape relied on (kind 9842, verified 2026-09-13):
    a           = repo coordinate "30617:<maintainer-hex>:<repo-id>"
                  (an event can carry several, e.g. one per maintainer)
    w           = [workflow path, sha256 of that file at the commit]
    c           = commit sha the run was performed at
    o           = trigger: push | pull_request | manual
    r           = [sha256 of the ref, ref name like refs/heads/<b>]
                  (there are TWO r tags, so match against all of them)
    conclusion  = success | failure | timed_out | startup_failure | ...
    queued_at / started_at = unix seconds

Filters
-------
* REPO_IDENTIFIER must appear in some `a` tag (matched strictly against the
  repo-id part of the coordinate, with a lenient substring fallback).
* --commit SHA  requires SHA in a `c` tag OR in any `r` tag.
* --ref REF     requires REF in any `r` tag (use the full ref name, e.g.
  refs/heads/main).

Exit codes (gate-meaningful)
----------------------------
    0  results found, every conclusion is success
    1  results found, at least one non-success conclusion
    2  NO results found  (no evidence is not green)
    3  `nak` CLI not found on PATH
    4  NO results found AND --require-conclusion was given
    5  tooling error (nak failed / timed out / unparseable output)

An event with no `conclusion` tag is reported as `unknown` and counts as a
non-success (exit 1): absence of a verdict is not a pass.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys

DEFAULT_RELAYS = ["wss://relay.ngit.dev", "wss://gitnostr.com"]
# 2026-09-22 (t_7c503759): 100 let a busy relay's newest-first stream saturate
# the window (136 events scanned, tollgate-module-basic-go's newest kind-9842
# outside it, gate read "no evidence"). 400 matches the measured window that
# covers the repo (verified success at -l 400); the server-side `#c` commit
# filter (see fetch_events) is the primary path and this is the fallback depth.
DEFAULT_LIMIT = 400
DEFAULT_TIMEOUT = 120

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NO_RESULTS = 2
EXIT_NAK_MISSING = 3
EXIT_NO_RESULTS_REQUIRED = 4
EXIT_TOOLING_ERROR = 5

SUCCESS = "success"


# --------------------------------------------------------------------------- #
# relay access
# --------------------------------------------------------------------------- #
def _nak_req(relays, limit, timeout, commit=None):
    """One `nak req` round trip. Returns (events, error_message).

    A server-side `-t c=<sha>` tag filter is added when `commit` is given, so a
    busy relay returns THIS commit's results instead of its newest `limit`
    events (see fetch_events).
    """
    cmd = ["nak", "req", "-k", "9842", "-l", str(limit)]
    if commit:
        cmd += ["-t", "c=%s" % commit]
    cmd += list(relays)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], "nak req timed out after %ss: %s" % (timeout, " ".join(cmd))
    except OSError as exc:  # pragma: no cover - defensive
        return [], "could not execute nak: %s" % exc

    events, seen, skipped = [], set(), 0
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        ev_id = ev.get("id")
        if ev_id and ev_id in seen:
            continue
        if ev_id:
            seen.add(ev_id)
        events.append(ev)

    stderr = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not events:
        return [], "nak req exited %s: %s" % (proc.returncode, stderr or "no output")
    if not events and not stderr:
        return [], "nak req returned no events and no diagnostics"
    if proc.returncode != 0:
        print(
            "warning: nak req exited %s but returned events; continuing"
            % proc.returncode,
            file=sys.stderr,
        )
    if skipped:
        print("warning: skipped %s unparseable lines" % skipped, file=sys.stderr)
    return events, None


def fetch_events(relays, limit, timeout, commit=None):
    """Return (events, error_message). Uses `nak req` only; no writes.

    Windowing note (t_7c503759, 2026-09-22): `-l <limit>` is applied by nak to
    the relay's stream, which is newest-first across ALL repositories. On a
    relay carrying a busy CI firehose, a repo's own older results fall out of
    that window entirely: measured 2026-09-19 with the then-default of 100 the
    tool saw 136 events and ZERO for tollgate-module-basic-go, while the same
    query at -l 400 returned its own results. A missing window reads as "no
    evidence" (exit 2/4) and blocks a gate that is in fact satisfied.

    So: when a commit is given, qualify the REQ server-side with `#c` first.
    If that yields nothing (e.g. a relay that ignores tag filters), fall back
    to the previous unqualified scan so the tool is never LESS sensitive than
    before.
    """
    events, err = _nak_req(relays, limit, timeout, commit=commit)
    if events or not commit or err:
        return events, err
    return _nak_req(relays, limit, timeout)


# --------------------------------------------------------------------------- #
# parsing / filtering
# --------------------------------------------------------------------------- #
def tag_values(event, name):
    out = []
    for tag in event.get("tags") or []:
        if tag and tag[0] == name and len(tag) > 1:
            out.append(tag[1])
    return out


def repo_matches(event, identifier):
    # Accept bare repo-id AND owner/name form: compare the last path segment
    # against the 30617 coordinate's repo-id part (t_7c503759, 2026-09-22).
    identifier_id = identifier.rsplit("/", 1)[-1]
    for value in tag_values(event, "a"):
        if value == identifier:
            return True
        parts = value.split(":", 2)
        if len(parts) == 3 and parts[0] == "30617" and parts[2] in (
                identifier, identifier_id):
            return True
        if identifier in value:  # lenient fallback
            return True
    return False


def normalise(event):
    """Flatten one 9842 event into the reported record."""
    w = tag_values(event, "w")
    r = tag_values(event, "r")
    c = tag_values(event, "c")
    o = tag_values(event, "o")
    conclusion = tag_values(event, "conclusion")
    # A ref NAME is the r value that is not a bare 64-hex digest.
    ref = ""
    for value in r:
        if value.startswith("refs/") or "/" in value or not _is_hex64(value):
            ref = value
            break
    if not ref and r:
        ref = r[0]
    return {
        "created_at": event.get("created_at"),
        "workflow": w[0] if w else "",
        "ref": ref,
        "commit": c[0] if c else "",
        "trigger": o[0] if o else "",
        "conclusion": conclusion[0] if conclusion else "unknown",
        "event_id": event.get("id", ""),
        "_r_all": r,
        "_c_all": c,
        "_a_all": tag_values(event, "a"),
        "_workflow_sha": w[1] if len(w) > 1 else "",
    }


def _is_hex64(value):
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower())


def collect(events, identifier, commit=None, ref=None):
    results = []
    for event in events:
        if not repo_matches(event, identifier):
            continue
        rec = normalise(event)
        if commit:
            if commit not in rec["_c_all"] and commit not in rec["_r_all"]:
                continue
        if ref:
            if ref not in rec["_r_all"]:
                continue
        results.append(rec)
    # newest first
    results.sort(key=lambda r: (r["created_at"] or 0, r["workflow"]), reverse=True)
    return results


def public(rec):
    """Record without the private helper keys."""
    return {k: v for k, v in rec.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def fmt_time(ts):
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def render_table(results, identifier, commit, ref):
    header = ["TIME (UTC)", "WORKFLOW", "REF", "TRIGGER", "CONCLUSION"]
    rows = [
        [
            fmt_time(r["created_at"]),
            r["workflow"] or "-",
            r["ref"] or "-",
            r["trigger"] or "-",
            r["conclusion"],
        ]
        for r in results
    ]
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))
    print(line.rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    print()
    tally = {}
    for r in results:
        tally[r["conclusion"]] = tally.get(r["conclusion"], 0) + 1
    scope = ["repo=%s" % identifier]
    if commit:
        scope.append("commit=%s" % commit)
    if ref:
        scope.append("ref=%s" % ref)
    print(
        "%d result(s) [%s]: %s"
        % (
            len(results),
            " ".join(scope),
            ", ".join("%s=%d" % (k, tally[k]) for k in sorted(tally)),
        )
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="ngit_ci_evidence.py",
        description=(
            "Read-only ngit (Nostr) CI evidence reader: queries kind-9842 "
            "workflow results and reports conclusions."
        ),
        epilog=(
            "exit codes: 0 all success | 1 any non-success | 2 no results | "
            "3 nak missing | 4 no results with --require-conclusion | "
            "5 tooling error"
        ),
    )
    p.add_argument("repo_identifier", help="repo id from the 30617 coordinate, "
                                           "e.g. tollgate-module-basic-go; the "
                                           "owner/name form (e.g. "
                                           "OpenTollGate/tollgate-module-basic-go) "
                                           "is accepted too")
    p.add_argument("--commit", default=None,
                   help="require this commit sha in the c tag or an r tag")
    p.add_argument("--ref", default=None,
                   help="require this ref (full name) in an r tag, "
                        "e.g. refs/heads/main")
    p.add_argument("--relays", nargs="+", default=list(DEFAULT_RELAYS),
                   metavar="URL", help="relay websocket URLs (default: %s)"
                   % " ".join(DEFAULT_RELAYS))
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="nak req limit (default: %d)" % DEFAULT_LIMIT)
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="emit a JSON list instead of a table")
    p.add_argument("--require-conclusion", action="store_true",
                   help="treat 'no results' as exit 4 instead of exit 2")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="nak timeout in seconds (default: %d)" % DEFAULT_TIMEOUT)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if shutil.which("nak") is None:
        print(
            "error: 'nak' CLI not found on PATH; install nak to read ngit CI "
            "evidence (no other query path is used)",
            file=sys.stderr,
        )
        return EXIT_NAK_MISSING

    events, err = fetch_events(args.relays, args.limit, args.timeout,
                               commit=args.commit)
    if err:
        print("error: %s" % err, file=sys.stderr)
        return EXIT_TOOLING_ERROR

    results = collect(events, args.repo_identifier, args.commit, args.ref)

    if args.as_json:
        print(json.dumps([public(r) for r in results], indent=2))
    elif results:
        render_table(results, args.repo_identifier, args.commit, args.ref)

    if not results:
        scope = ["repo=%s" % args.repo_identifier]
        if args.commit:
            scope.append("commit=%s" % args.commit)
        if args.ref:
            scope.append("ref=%s" % args.ref)
        print(
            "no kind-9842 CI results found for %s "
            "(%d event(s) scanned from %s)"
            % (" ".join(scope), len(events), ", ".join(args.relays)),
            file=sys.stderr,
        )
        return EXIT_NO_RESULTS_REQUIRED if args.require_conclusion else EXIT_NO_RESULTS

    if all(r["conclusion"] == SUCCESS for r in results):
        return EXIT_OK
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
