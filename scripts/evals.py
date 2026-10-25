"""Evaluation run: real customer questions through the live chat API, graded without a model.

    python -m scripts.evals                         # all cases against http://127.0.0.1:8000
    python -m scripts.evals --only dispute_confirmed_by_voice_style_yes
    python -m scripts.evals --pause 12              # seconds between turns (free-tier rate limits)

Checks are deterministic: tools called, exact figures and rule IDs in the reply, actions proposed or
executed, and text that must not appear. Results go to docs/EVALS.md and data/eval_results.json.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases.yaml"


def normalise(text: str) -> str:
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"[‐‑‒–—]", "-", text)
    return re.sub(r"\s+", " ", text).lower()


def run_turn(client: httpx.Client, token: str, message: str) -> dict:
    turn = {"text": "", "tools": [], "proposed": [], "executed": [], "rules": [], "metrics": None, "error": None}
    with client.stream("POST", "/api/chat", json={"message": message}, headers={"Authorization": f"Bearer {token}"},
                       timeout=180) as response:
        if response.status_code != 200:
            turn["error"] = f"HTTP {response.status_code}"
            return turn
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            e = json.loads(line[6:])
            kind = e["type"]
            if kind == "text_delta":
                turn["text"] += e["text"]
            elif kind == "tool_end":
                turn["tools"].append(e["name"])
            elif kind == "action_proposed":
                turn["proposed"].append(e["action"])
            elif kind == "action_updated" and e["action"]["status"] == "executed":
                turn["executed"].append(e["action"]["type"])
            elif kind == "evidence":
                turn["rules"] = [i["id"] for i in e["items"] if i["type"] == "rule"]
            elif kind == "error":
                turn["error"] = e["message"]
            elif kind == "turn_end":
                turn["metrics"] = e["metrics"]
    return turn


def grade(expect: dict, turn: dict) -> list[str]:
    """Returns the failed checks (empty when the turn passes)."""
    failures = []
    reply = normalise(turn["text"])
    cited = set(re.findall(r"\b[A-Z]{2,4}(?:-[A-Z]+)?-\d+\b", turn["text"].replace("‑", "-")))
    if turn["error"]:
        failures.append(f"error: {turn['error']}")
    for name in expect.get("tools", []):
        if name not in turn["tools"]:
            failures.append(f"did not call {name} (called {turn['tools'] or 'nothing'})")
    if expect.get("tools_any") and not set(expect["tools_any"]) & set(turn["tools"]):
        failures.append(f"called none of {expect['tools_any']}")
    if expect.get("no_tools") and turn["tools"]:
        failures.append(f"called tools {turn['tools']} but should not")
    for text in expect.get("contains", []):
        if normalise(text) not in reply:
            failures.append(f"reply missing '{text}'")
    if expect.get("contains_any") and not any(normalise(t) in reply for t in expect["contains_any"]):
        failures.append(f"reply has none of {expect['contains_any']}")
    for text in expect.get("not_contains", []):
        if normalise(text) in reply:
            failures.append(f"reply contains forbidden '{text}'")
    for rule in expect.get("cites", []):
        if rule not in cited:
            failures.append(f"did not cite {rule}")
    if expect.get("cites_any") and not set(expect["cites_any"]) & cited:
        failures.append(f"cited none of {expect['cites_any']} (cited {sorted(cited) or 'nothing'})")
    proposal = expect.get("action_proposed")
    if proposal:
        match = [a for a in turn["proposed"] if a["type"] == proposal["type"]]
        if not match:
            failures.append(f"no {proposal['type']} action proposed")
        elif "requires_otp" in proposal and match[0]["requires_otp"] != proposal["requires_otp"]:
            failures.append(f"{proposal['type']} requires_otp={match[0]['requires_otp']}")
    if expect.get("action_executed") and expect["action_executed"] not in turn["executed"]:
        failures.append(f"{expect['action_executed']} was not executed")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--pause", type=float, default=12)
    parser.add_argument("--only", nargs="*", help="Run only these case IDs")
    args = parser.parse_args()
    cases = yaml.safe_load(CASES.read_text())
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    results = []
    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        config = client.get("/api/config").json()
        if not config["llm_configured"]:
            raise SystemExit("The app has no model configured. Add a key to .env and restart (make run).")
        client.post("/api/ops/reset")
        for i, case in enumerate(cases):
            session = client.post("/api/session", json={"customer_id": case["customer"],
                                                        "usecase": case.get("workspace", "loan_servicing")}).json()
            turns, failures = [], []
            for n, step in enumerate(case["turns"]):
                if turns or i:
                    time.sleep(args.pause)
                turn = run_turn(client, session["token"], step["say"])
                if turn["error"] and "rate limit" in turn["error"].lower():
                    time.sleep(35)  # free-tier per-minute cap: the failed turn was rolled back, so retry it once
                    turn = run_turn(client, session["token"], step["say"])
                turn_failures = grade(step.get("expect", {}), turn)
                failures += [f"turn {n + 1}: {f}" for f in turn_failures]
                turns.append({"say": step["say"], "reply": turn["text"], "tools": turn["tools"],
                              "total_ms": (turn["metrics"] or {}).get("total_ms"), "failures": turn_failures})
            passed = not failures
            results.append({"id": case["id"], "category": case["category"], "passed": passed, "failures": failures,
                            "turns": turns})
            print(f"{'PASS' if passed else 'FAIL'}  {case['id']}" + ("" if passed else f"  -> {'; '.join(failures)}"))
    if not args.only:  # a partial run would misstate the overall result
        write_report(results, config, args)
    total = sum(r["passed"] for r in results)
    print(f"\n{total}/{len(results)} cases passed")
    sys.exit(0 if total == len(results) else 1)


def write_report(results: list[dict], config: dict, args):
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "eval_results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    passed = sum(r["passed"] for r in results)
    categories: dict[str, list[bool]] = {}
    for r in results:
        categories.setdefault(r["category"], []).append(r["passed"])
    lines = [
        "# Evaluations", "",
        "Real customer questions run through the live chat API and graded without a model: the right tools must be "
        "called, the exact figures and policy rule IDs must appear in the reply, actions must be proposed or executed "
        "as expected, and some text must never appear (another customer's data, an OTP that does not exist). "
        "Cases are in [`evals/cases.yaml`](../evals/cases.yaml); run them with `make eval` while the app is running.",
        "", "Expected figures come from the synthetic data and do not change with the business date, so a run on any "
        "day is comparable. Models are not deterministic, so a case can occasionally flip between runs; treat a "
        "failure as something to read, not just a number. What earlier runs found and what changed because of them: "
        "[`evals/FINDINGS.md`](../evals/FINDINGS.md).", "",
        f"## Latest run: {passed}/{len(results)} passed", "",
        f"{datetime.now():%d %b %Y %H:%M}, model {config['model']}, {args.pause:.0f} s between turns.", "",
        "| Category | Passed |", "|---|---|"]
    for category, outcomes in categories.items():
        lines.append(f"| {category} | {sum(outcomes)}/{len(outcomes)} |")
    lines += ["", "| Case | Result | Tools called | Notes |", "|---|---|---|---|"]
    for r in results:
        tools = ", ".join(dict.fromkeys(t for turn in r["turns"] for t in turn["tools"])) or "none"
        note = "; ".join(r["failures"]) if r["failures"] else ""
        lines.append(f"| {r['id']} | {'pass' if r['passed'] else '**fail**'} | {tools} | {note} |")
    (ROOT / "docs" / "EVALS.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
