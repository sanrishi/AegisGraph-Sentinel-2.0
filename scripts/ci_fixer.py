import os
import base64
import subprocess
import requests
from groq import Groq

client_groq = Groq(api_key=os.environ["GROQ_API_KEY"])

GH_TOKEN = os.environ["GH_TOKEN"]
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "sanrishi")

UPSTREAM_REPOS = [
    "Puneet04-tech/AegisGraph-Sentinel-2.0",
    "leonagoel/hybrid-recommender",
]

HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}


def get_open_prs(repo):
    url = f"https://api.github.com/repos/{repo}/pulls"
    params = {"state": "open"}
    resp = requests.get(url, headers=HEADERS, params=params)
    if resp.status_code != 200:
        print(f"Could not get PRs for {repo}: {resp.status_code}")
        return []
    all_prs = resp.json()
    mine = [pr for pr in all_prs
            if pr.get("user", {}).get("login") == GITHUB_USERNAME]
    print(f"Total open PRs: {len(all_prs)}, yours: {len(mine)}")
    return mine


def get_failed_check_runs(repo, sha):
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        return []
    runs = resp.json().get("check_runs", [])
    return [r for r in runs if r.get("conclusion") == "failure"]


def get_failure_logs(repo, details_url):
    try:
        run_id = details_url.split("/runs/")[1].split("/")[0]
    except Exception:
        return ""

    result = subprocess.run(
        ["gh", "run", "view", run_id, "--repo", repo, "--log-failed"],
        capture_output=True,
        text=True,
        env={**os.environ, "GH_TOKEN": GH_TOKEN}
    )
    logs = result.stdout + result.stderr
    lines = logs.strip().split('\n')
    # Keep only last 80 lines to stay under token limit
    return '\n'.join(lines[-80:])


def get_changed_py_files(repo, pr_number):
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        return []
    return [
        f["filename"] for f in resp.json()
        if f["filename"].endswith(".py")
        and not f["filename"].startswith("tests/")
    ][:2]


def get_file_content_and_sha(repo, filepath, branch):
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    resp = requests.get(url, headers=HEADERS, params={"ref": branch})
    if resp.status_code != 200:
        return None, None
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def strip_markdown_fences(code):
    """Remove markdown code fences that LLMs sometimes add despite being told not to."""
    lines = code.strip().splitlines()
    # Drop leading ```python or ``` line
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    # Drop trailing ``` line
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def fix_with_groq(failure_log, filepath, original_code):
    original_line_count = len(original_code.splitlines())

    # Send the full file — truncating caused Groq to return a shortened
    # "complete" file, deleting everything beyond the truncation point.
    # We raise max_tokens to accommodate large files.
    prompt = f"""Python CI test is failing. Fix ONLY the lines causing the failure.

FAILURE LOG (last 80 lines):
{failure_log}

FILE TO FIX ({filepath}):
{original_code}

Rules:
- Return ONLY the complete fixed Python file, no explanation, no markdown, no backticks.
- Do NOT remove, rewrite, or shorten any code that is unrelated to the failure.
- The returned file MUST have at least as many lines as the original ({original_line_count} lines).
- If this file is not the cause of the failure, return it completely unchanged."""

    response = client_groq.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8000
    )
    fixed_code = response.choices[0].message.content.strip()

    # Strip markdown fences in case the model ignored instructions
    fixed_code = strip_markdown_fences(fixed_code)

    return fixed_code


def push_fix(fork_repo, branch, filepath, fixed_code, file_sha):
    url = f"https://api.github.com/repos/{fork_repo}/contents/{filepath}"
    data = {
        "message": f"fix: auto-fix CI failure in {filepath} via groq [skip ci]",
        "content": base64.b64encode(fixed_code.encode()).decode(),
        "branch": branch,
        "sha": file_sha
    }
    resp = requests.put(url, headers=HEADERS, json=data)
    return resp.status_code in [200, 201]


def main():
    for repo in UPSTREAM_REPOS:
        fork_repo = f"{GITHUB_USERNAME}/{repo.split('/')[1]}"
        print(f"\n🔍 Checking {repo}...")

        prs = get_open_prs(repo)
        print(f"Found {len(prs)} open PRs")

        for pr in prs:
            pr_number = pr["number"]
            branch = pr["head"]["ref"]
            sha = pr["head"]["sha"]
            print(f"\nChecking PR #{pr_number}: {pr['title'][:60]}")

            failed_runs = get_failed_check_runs(repo, sha)
            if not failed_runs:
                print(f"✅ PR #{pr_number}: No failures")
                continue

            print(f"❌ PR #{pr_number}: {len(failed_runs)} failed checks")

            details_url = failed_runs[0].get("details_url", "")

            # BUG FIX: this entire block was missing — the file was cut off above
            failure_log = get_failure_logs(repo, details_url)
            if not failure_log.strip():
                print(f"⚠️  PR #{pr_number}: Could not retrieve failure logs, skipping")
                continue

            py_files = get_changed_py_files(repo, pr_number)
            if not py_files:
                print(f"⚠️  PR #{pr_number}: No fixable Python files found in PR diff")
                continue

            print(f"📂 Files to fix: {py_files}")

            for filepath in py_files:
                print(f"🔧 Attempting fix for {filepath}...")

                original_code, file_sha = get_file_content_and_sha(
                    fork_repo, filepath, branch
                )
                if original_code is None:
                    print(f"⚠️  Could not fetch {filepath} from {fork_repo}@{branch}")
                    continue

                fixed_code = fix_with_groq(failure_log, filepath, original_code)

                if fixed_code.strip() == original_code.strip():
                    print(f"ℹ️  No changes suggested for {filepath}")
                    continue

                # Safety check: reject if Groq returned significantly fewer lines
                # than the original — this means it deleted code, not fixed it
                original_lines = len(original_code.splitlines())
                fixed_lines = len(fixed_code.splitlines())
                if fixed_lines < original_lines * 0.85:
                    print(
                        f"🚫 Rejected fix for {filepath}: "
                        f"Groq returned {fixed_lines} lines vs original {original_lines} "
                        f"(more than 15% shorter — likely deleted code, not fixed it)"
                    )
                    continue

                success = push_fix(fork_repo, branch, filepath, fixed_code, file_sha)
                if success:
                    print(f"✅ Pushed fix for {filepath} to {fork_repo}@{branch}")
                else:
                    print(f"❌ Failed to push fix for {filepath}")

    print("\n✅ CI fixer run complete.")


# BUG FIX: this entry point was missing — without it main() is never called
if __name__ == "__main__":
    main()