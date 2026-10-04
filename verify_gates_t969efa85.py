import json, sys
sys.path.insert(0, "/home/c03rad0r/.hermes/bot/governance")
import gate_engine as ge

r = ge.evaluate_task("plebeian-pr-reviews", "t_969efa85")
keep = {k: r.get(k) for k in ["tier", "verdict", "passed", "missing", "author_family",
                              "cross_family", "ci_evidence", "review_artifact",
                              "review_published", "consolidated", "review_benchmark_floor",
                              "secrets_hits", "pr_branch_naming"]}
print(json.dumps(keep, indent=1))
