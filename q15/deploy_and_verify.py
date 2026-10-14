#!/usr/bin/env python3
"""Deploy helper + live verifier for the Acme ledger Q&A service.

Two independent steps:

  A) push_to_github(repo)  -- create a GitHub repo and push this folder to it
     using a GitHub Personal Access Token (classic, `repo` scope) or `gh` CLI.

  B) verify_live(url)      -- once Render gives you the public URL, verify the
     live server exactly like the grader: POST {"question": ...} several times
     with different phrasings and check each answer against known references.

Usage:
  # 1) Push to GitHub (choose ONE auth method):
  export GITHUB_TOKEN=ghp_xxx            # classic PAT with `repo` scope
  python deploy_and_verify.py push tds-ga1-q15-ledger-qa

  #   ...or, if you have the GitHub CLI logged in:
  python deploy_and_verify.py push tds-ga1-q15-ledger-qa

  # 2) After deploying on Render, verify the live endpoint:
  python deploy_and_verify.py verify https://<service>.onrender.com
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# (question, expected_answer). These are computed from the (deduped, IST-dated,
# USD-converted) ledger and act as a regression suite for the live endpoint.
CASES = [
    ("What was the total revenue in USD from the North region in March 2026?", 37382.56),
    ("Total revenue from the North region in March 2026", 37382.56),
    ("What is the total revenue for all time?", 1066390.12),
    ("How much revenue did we make in 2026?", 1066390.12),
    ("What was the revenue in Q1 2026?", 528461.18),
    ("How much was refunded in the West region?", 30937.53),
    ("What were the refunds in North in March 2026?", 5968.84),
    ("How many refunds were there?", 451),
    ("How many orders were placed in April 2026?", 648),
    ("How many distinct customers made purchases?", 250),
    ("How many units were sold in total?", 9695),
    ("What was the top selling product by revenue?", "Blender"),
    ("What was the average order value?", 331.80),
    ("What was the net revenue overall?", 916613.52),
]

# --------------------------------------------------------------------------- #
# A) Push to GitHub
# --------------------------------------------------------------------------- #

def push_to_github(repo: str) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    owner = os.environ.get("GITHUB_OWNER", "pushpendra457")

    if token:
        req = urllib.request.Request(
            "https://api.github.com/user/repos",
            data=json.dumps({"name": repo, "private": False}).encode(),
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                print(f"Created repo: {json.load(resp)['html_url']}")
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 422 and "already exists" in body:
                print("Repo already exists; continuing to push.")
            else:
                raise SystemExit(f"GitHub API error {e.code}: {body}")

        remote = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    else:
        if subprocess.run(["bash", "-lc", "command -v gh"], capture_output=True).returncode != 0:
            raise SystemExit(
                "No GITHUB_TOKEN set and `gh` not found. Install/authorize gh, "
                "or export GITHUB_TOKEN=ghp_... with `repo` scope."
            )
        subprocess.run(["gh", "repo", "create", repo, "--public", "--source", ".",
                        "--remote", "origin", "--push"], cwd=HERE, check=True)
        print("Pushed via gh CLI.")
        print("\nNext: deploy this repo on Render (New + -> Blueprint).")
        return

    subprocess.run(["git", "init", "-q"], cwd=HERE, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=HERE, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Acme ledger Q&A service"], cwd=HERE,
                   capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=HERE, capture_output=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=HERE, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=HERE, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=HERE, check=True)
    print(f"\nPushed to https://github.com/{owner}/{repo}")
    print("Next: Render -> New + -> Blueprint -> select this repo -> Apply.")


# --------------------------------------------------------------------------- #
# B) Verify a live endpoint exactly like the grader
# --------------------------------------------------------------------------- #

def _post(url: str, payload: dict, timeout: float = 15.0):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else None), time.time() - t0
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, (json.loads(body) if body else None), time.time() - t0


def verify_live(base_url: str) -> int:
    base_url = base_url.rstrip("/")

    # Warm up (cold start on free tier + first dataset fetch).
    print("Warming up endpoint (cold start + dataset fetch)...")
    code, data, dt = _post(base_url, {"question": "total revenue"}, timeout=60)
    print(f"  warmup -> {code} {data} in {dt:.1f}s")

    failures = 0
    for question, expected in CASES:
        code, data, dt = _post(base_url, {"question": question})
        got = data.get("answer") if isinstance(data, dict) else None
        ok = code == 200 and (
            abs(got - expected) < 0.005 if isinstance(expected, float) and isinstance(got, (int, float))
            else got == expected
        )
        late = dt > 15.0
        if not ok or late:
            failures += 1
        flag = "OK  " if ok and not late else "FAIL"
        print(f"[{flag}] {dt*1000:6.0f}ms  {question!r} -> {got!r} (want {expected!r})")

    print("\nLIVE RESULT:", "ALL PASSED" if failures == 0 else f"{failures} FAILURES")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "push":
        push_to_github(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "verify":
        sys.exit(verify_live(sys.argv[2]))
    else:
        print(__doc__)
        sys.exit(2)

