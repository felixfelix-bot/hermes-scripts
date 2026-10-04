"""GREEN test for the review-lane dispatch fix (fleet_scheduler._repo_for_board).

Regression: a work-lane board that is not itself a repo (plebeian-pr-reviews)
produced tasks tagged `repo:<board>`, which no node's fit profile contains, so
every node skipped them (`unfit: missing repo:<board>`) and the cards stranded
in `ready` forever with no review ever published on the PR.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import fleet_queue as fq  # noqa: E402
import fleet_scheduler as fs  # noqa: E402

FIT = json.load(open(os.path.expanduser("~/.hermes/bot/fleet_fit.json")))


def test_lane_board_maps_to_its_repo():
    assert fs._repo_for_board("plebeian-pr-reviews") == "market"
    assert fs._repo_for_board("plebeian-my-prs") == "market"
    assert fs._repo_for_board("plebeian") == "market"


def test_repo_named_board_is_unchanged():
    assert fs._repo_for_board("market") == "market"
    assert fs._repo_for_board("balloon-fresh") == "balloon-fresh"
    assert fs._repo_for_board("fleet") == ""


def test_lane_board_task_is_now_routable_not_skipped():
    repo = fs._repo_for_board("plebeian-pr-reviews")
    task = fq.classify("plebeian-pr-reviews", repo, "Review PR #1318: fix(auctions)", "")
    assert task["requires"] == ["repo:market"], task["requires"]
    decision, why = fq.route(task, {"headroom_score": 9.0}, [], FIT, 0.0)
    assert not (decision == "skip" and "unfit" in why), (decision, why)


def test_worker_assignee_wins_over_heuristics():
    prof = fs._profile_for({"title": "Review PR #1318: fix(auctions)",
                            "body": "", "assignee": "worker-reviewer-kimi"})
    assert prof == "worker-reviewer-kimi", prof


def test_non_worker_assignee_is_ignored():
    prof = fs._profile_for({"title": "Review PR #1318: fix(auctions)",
                            "body": "", "assignee": "manager"})
    assert prof != "manager", prof
    assert prof.startswith("worker-"), prof


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
