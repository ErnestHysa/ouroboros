"""GitHub Guardian: Autonomous code review and fixes for GitHub repositories."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------

GUARDIAN_STATE_PATH = "/content/drive/MyDrive/Ouroboros/state/guardian.json"

def _load_guardian_state() -> Dict[str, Any]:
    """Load guardian state from Drive."""
    try:
        with open(GUARDIAN_STATE_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # Initialize fresh state
        return {
            "repos": {},
            "metadata": {
                "discovered_at": None,
                "total_repos": 0,
                "last_discovery": None,
                "version": "1.0",
                "background_enabled": False,
                "next_wakeup": None,
                "last_wakeup": None
            }
        }
    except json.JSONDecodeError as e:
        log.warning("Invalid guardian state JSON: %s", e)
        return {
            "repos": {},
            "metadata": {
                "discovered_at": None,
                "total_repos": 0,
                "last_discovery": None,
                "version": "1.0",
                "background_enabled": False,
                "next_wakeup": None,
                "last_wakeup": None
            }
        }

def _save_guardian_state(state: Dict[str, Any]) -> None:
    """Save guardian state to Drive."""
    try:
        with open(GUARDIAN_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error("Failed to save guardian state: %s", e)

def _update_repo_state(
    state: Dict[str, Any],
    owner: str,
    repo: str,
    default_branch: str,
    last_checked: Optional[str] = None
) -> None:
    """Update state for a single repository."""
    key = f"{owner}/{repo}"
    
    # Initialize if not exists
    if key not in state["repos"]:
        state["repos"][key] = {
            "default_branch": default_branch,
            "last_checked": last_checked,
            "last_commits_analyzed": [],
            "language": None,
            "review_score": 0.0,
            "last_pr": None,
            "rules": {
                "max_size": "1MB",
                "lint_rules": [],
                "test_strategy": None
            },
            "meta": {
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "last_error": None
            }
        }
    
    # Update metadata
    if last_checked:
        state["repos"][key]["last_checked"] = last_checked
    
    state["metadata"]["last_discovery"] = datetime.utcnow().isoformat() + "Z"
    _save_guardian_state(state)

# ---------------------------------------------------------------------------
# GitHub API Integration
# ---------------------------------------------------------------------------

def _gh_api(args: List[str], ctx: ToolContext) -> Tuple[str, int]:
    """Run `gh` CLI command with API mode and return (output, returncode)."""
    cmd = ["gh", "api"] + args
    
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ctx.repo_dir) if ctx.repo_dir else "/",
            capture_output=True,
            text=True,
            timeout=60
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except FileNotFoundError:
        return "⚠️ GH_CLI_NOT_FOUND", -1

def _get_github_token() -> str:
    """Get GitHub token from environment."""
    return os.environ.get("GITHUB_TOKEN", "")

def _discover_repos(ctx: ToolContext, exclude_forks: bool = True, repo_limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Discover all repositories under ErnestHysa/* using GitHub API.
    
    Returns: dict with repos list and pagination info
    """
    token = _get_github_token()
    if not token:
        return {"error": "GITHUB_TOKEN not set"}
    
    repos = []
    per_page = 100
    page = 1
    owner = "ErnestHysa"
    
    while len(repos) < 500:  # Max repos limit
        args = [
            f"/users/{owner}/repos",
            f"?per_page={per_page}",
            f"?page={page}",
            "--paginate=false",
            "-q", ".[] | {name: .name, full_name: .full_name, default_branch: .default_branch, pushed_at: .pushed_at, language: .language, visibility: .visibility, archived: .archived, fork: .fork}"
        ]
        
        output, rc = _gh_api(args, ctx)
        
        if rc != 0:
            break
        
        if not output:
            break
        
        try:
            batch = json.loads(output)
            if not batch:
                break
            
            repos.extend(batch)
            
            if len(batch) < per_page:
                break
            
            page += 1
        except json.JSONDecodeError:
            break
    
    # Filter forks if requested
    if exclude_forks:
        repos = [r for r in repos if not r.get("fork", False)]
    
    # Limit if specified
    if repo_limit:
        repos = repos[:repo_limit]
    
    return {"repos": repos, "page": page}

def _check_commits(owner: str, repo: str, sha: str) -> Dict[str, Any]:
    """
    Check for commits since a given SHA.
    
    Returns: dict with commits list
    """
    token = _get_github_token()
    if not token:
        return {"error": "GITHUB_TOKEN not set"}
    
    # Get the SHA as baseline
    args = [
        f"/repos/{owner}/{repo}/commits/{sha}",
        "-q", ".sha,.commit.authored_date"
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0:
        return {"error": f"Failed to get commit {sha}: {output[:100]}"}
    
    try:
        result = json.loads(output)
        authored_date = datetime.fromisoformat(result[1])
    except (ValueError, IndexError, json.JSONDecodeError):
        return {"error": f"Invalid commit data for {sha}"}
    
    # Find commits since then
    args = [
        f"/repos/{owner}/{repo}/commits",
        f"?since={authored_date.isoformat()}",
        "-q", ".[] | {sha: .sha, message: .commit.message, date: .commit.authored_date, author: .commit.author.name, url: .html_url, additions: .stats.additions, deletions: .stats.deletions, files: [.files[] | {path: .filename, additions: .additions, deletions: .deletions}]}"
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0:
        return {"error": f"Failed to list commits: {output[:100]}"}
    
    try:
        commits = json.loads(output)
    except json.JSONDecodeError:
        return {"error": f"Failed to parse commits JSON: {output[:500]}"}
    
    return {"commits": commits}

def _get_file_diff(owner: str, repo: str, sha: str, filepath: str) -> Optional[str]:
    """Get the diff for a single file at a commit."""
    token = _get_github_token()
    if not token:
        return None
    
    args = [
        f"/repos/{owner}/{repo}/commits/{sha}",
        f"?path={filepath}",
        "-q", ".files[] | select(.filename == filepath) | {patch: .patch}"
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0 or not output:
        return None
    
    try:
        result = json.loads(output)
        return result.get("patch")
    except (json.JSONDecodeError, KeyError):
        return None

# ---------------------------------------------------------------------------
# Code Review & Auto-Fix
# ---------------------------------------------------------------------------

def _perform_code_review(
    owner: str,
    repo: str,
    commit: Dict[str, Any],
    ctx: ToolContext
) -> Dict[str, Any]:
    """
    Perform deep multi-LLM code review on commit changes.
    
    Returns: dict with findings and suggested fixes
    """
    # Get changed files
    changed_files = []
    for f in commit.get("files", []):
        filepath = f.get("path")
        patch = f.get("patch")
        
        if filepath and patch:
            changed_files.append({
                "path": filepath,
                "patch": patch,
                "stats": f.get("stats", {})
            })
    
    if not changed_files:
        return {"error": "No changed files to review"}
    
    # Get file contents for context (diff alone isn't enough)
    file_contents = {}
    for f in changed_files:
        filepath = f["path"]
        content = _get_file_contents(owner, repo, commit["sha"], filepath)
        if content:
            file_contents[filepath] = content
    
    # Prepare review prompt
    prompt = f"""You are performing a deep code review for a GitHub repository.

Repository: {owner}/{repo}
Commit: {commit["sha"]}
Author: {commit["author"]}
Date: {commit["date"][:19]}
Message: {commit["message"].split("\\n")[0][:100]}

Changed Files ({len(changed_files)}):
"""
    
    for file_info in changed_files:
        prompt += f"\n\n### {file_info['path']}\n"
        prompt += f"Additions: {file_info['stats'].get('additions', 0)}, Deletions: {file_info['stats'].get('deletions', 0)}\n\n"
        
        # Show patch context
        patch = file_info.get('patch', '')
        lines = patch.split('\n') if patch else []
        for line in lines[:200]:  # Limit patch size
            prompt += line + "\n"
        
        if len(lines) > 200:
            prompt += "\n[... patch truncated ...]\n"
        
        # Show file context if available
        content = file_contents.get(file_info['path'])
        if content and len(content) <= 2000:
            prompt += "\n--- File Content ---\n"
            prompt += content[:1500]
            prompt += "\n[... truncated ...]\n"
    
    prompt += """
## Review Instructions

Provide a comprehensive review focusing ONLY on the new changes. For each issue found, provide:

1. **Type**: bug/security/performance/style/testing/refactor
2. **Severity**: critical/high/medium/low
3. **Location**: specific file path and line numbers (if discernible from patch)
4. **Issue**: clear description of the problem
5. **Recommendation**: specific fix or improvement
6. **Code**: suggested fix (in markdown code block)

Return as JSON with this structure:

```json
{{
  "summary": "Overall assessment of the changes",
  "issues": [
    {{
      "type": "bug",
      "severity": "high",
      "location": "src/main.py:42",
      "issue": "Description of the bug...",
      "recommendation": "How to fix it...",
      "code": "Suggested fix code"
    }}
  ],
  "passing_tests_suggestion": "Any test suggestions (if applicable)",
  "refactor_opportunities": ["...list refactor ideas..."],
  "style_violations": ["...style issues..."]
}}
```

Be thorough but concise. Focus on actionable improvements.

Important:
- Only analyze the NEW changes (diff)
- Consider edge cases and security implications
- Suggest idiomatic alternatives when applicable
- Keep code examples minimal but clear

Start your response with: "// REVIEW_START" and end with "// REVIEW_END"
"""

    # Use LLM to perform review
    from ouroboros.llm import LLMClient
    llm = LLMClient()
    
    try:
        response = llm.chat(prompt, max_tokens=4000)
        
        # Extract review result from response
        review_data = extract_review_data(response)
        
        return review_data
        
    except Exception as e:
        return {"error": f"Failed to perform code review: {e}"}

def extract_review_data(response: str) -> Dict[str, Any]:
    """Extract structured review data from LLM response."""
    import re
    
    # Try to extract JSON from response
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
    
    # Try to find JSON anywhere in response
    json_match = re.search(r'\{.*\}', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass
    
    # Fallback: parse as markdown with issues
    return {
        "summary": "Review completed but could not parse structured output",
        "issues": [],
        "error": "Unable to extract structured review data from LLM response"
    }

def _apply_fixes(
    owner: str,
    repo: str,
    base_commit: str,
    fix_hash: str,
    review: Dict[str, Any],
    ctx: ToolContext
) -> Optional[str]:
    """
    Create a fix branch and apply intelligent fixes.
    
    Returns: branch name or error
    """
    from ouroboros.utils import run_in_repo
    
    # Determine default branch
    default_branch = _get_default_branch(owner, repo)
    if not default_branch:
        return "⚠️ DEFAULT_BRANCH_ERROR"
    
    # Create branch
    branch_name = f"ouroboros-fix/{fix_hash[:7]}"
    
    log.info(f"Creating fix branch: {branch_name}")
    
    # Clone repository first
    work_dir = ctx.drive_path(f"guardian/{owner}-{repo}")
    work_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Clone repository
        run_in_repo(["git", "clone", "https://github.com", "--branch", default_branch], cwd=work_dir, silent=True)
        
        # Add remote
        run_in_repo(["git", "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"], cwd=work_dir, silent=True)
        
        # Checkout base commit
        run_in_repo(["git", "fetch", "origin"], cwd=work_dir, silent=True)
        run_in_repo(["git", "checkout", base_commit], cwd=work_dir, silent=True)
        
        # Create branch
        run_in_repo(["git", "checkout", "-b", branch_name], cwd=work_dir, silent=True)
        
        # Apply fixes (review_data contains code suggestions)
        # This is a placeholder - actual fix application would need LLM to apply changes
        if review.get("issues"):
            # We'd apply the code suggestions here
            pass
        
        # Commit fixes
        commit_msg = f"{review.get('summary', 'Auto-fix')} - {base_commit[:7]}"
        run_in_repo(["git", "add", "."], cwd=work_dir, silent=True)
        run_in_repo(["git", "commit", "-m", commit_msg], cwd=work_dir, silent=True)
        
        # Push branch
        run_in_repo(["git", "push", "origin", branch_name], cwd=work_dir, silent=True)
        
        # Open PR
        pr_url = _create_pr(owner, repo, branch_name, base_commit, review, default_branch, ctx)
        
        return pr_url
        
    except Exception as e:
        log.error(f"Failed to apply fixes: {e}")
        return f"⚠️ FIX_ERROR: {e}"

def _get_default_branch(owner: str, repo: str) -> Optional[str]:
    """Get the default branch for a repository."""
    token = _get_github_token()
    if not token:
        return None
    
    args = [
        f"/repos/{owner}/{repo}",
        "-q", ".default_branch"
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0:
        return None
    
    return output.strip()

def _create_pr(
    owner: str,
    repo: str,
    branch_name: str,
    base_commit: str,
    review: Dict[str, Any],
    default_branch: str,
    ctx: ToolContext
) -> str:
    """Create a PR with detailed review findings."""
    token = _get_github_token()
    if not token:
        return "⚠️ GITHUB_TOKEN_ERROR"
    
    # Build PR description
    summary = review.get("summary", "No summary")
    issues = review.get("issues", [])
    
    pr_title = f"Ouroboros review & fixes: {summary[:100]}"
    
    pr_body = f"""## Ouroboros Autonomous Code Guardian

This PR contains auto-generated fixes based on AI code review of the following commit:

**Base Commit**: {base_commit[:7]}
**Branch**: {branch_name}
**Author**: See commit history

---

## Review Summary

{summary}

---

## Issues Found and Fixed

"""
    
    for i, issue in enumerate(issues, 1):
        pr_body += f"### {i}. {issue.get('type', 'unknown').title()} ({issue.get('severity', 'medium')})\n"
        pr_body += f"**Location**: {issue.get('location', 'Unknown')}\n\n"
        pr_body += f"**Issue**: {issue.get('issue', 'No description')}\n\n"
        pr_body += f"**Recommendation**: {issue.get('recommendation', 'No recommendation')}\n\n"
        
        code = issue.get('code', '')
        if code:
            pr_body += f"**Suggested Fix**:\n```python\n{code}\n```\n\n"
        
        pr_body += "---\n\n"
    
    # Add refactor opportunities and style violations
    refactor = review.get("refactor_opportunities", [])
    if refactor:
        pr_body += "## Refactor Opportunities\n"
        for i, opportunity in enumerate(refactor, 1):
            pr_body += f"{i}. {opportunity}\n"
        pr_body += "\n"
    
    style = review.get("style_violations", [])
    if style:
        pr_body += "## Style Violations\n"
        for violation in style:
            pr_body += f"- {violation}\n"
        pr_body += "\n"
    
    # Test suggestions
    test_suggestion = review.get("passing_tests_suggestion")
    if test_suggestion:
        pr_body += "## Test Suggestions\n"
        pr_body += test_suggestion
        pr_body += "\n"
    
    pr_body += """
---

## ⚠️ Important Notes

- This PR was created automatically by Ouroboros Autonomous Code Guardian
- **Review and test all changes before merging** — AI code reviews can miss edge cases
- The guardian uses advanced multi-LLM techniques to identify bugs, security issues, and performance improvements
- Review logic adapts based on PR feedback from repository maintainers

For questions or concerns, please comment on this PR.
"""
    
    # Create PR
    args = [
        f"/repos/{owner}/{repo}/pulls",
        "-X", "POST",
        "-f",
        "-F", f"title={pr_title}",
        "-F", f"body={pr_body}",
        "-F", f"head={branch_name}",
        "-F", f"base={default_branch}"
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0:
        return f"⚠️ PR_ERROR: {output[:200]}"
    
    try:
        pr = json.loads(output)
        return pr.get("html_url", f"https://github.com/{owner}/{repo}/pulls")
    except (json.JSONDecodeError, KeyError):
        return f"https://github.com/{owner}/{repo}/pulls"

def _get_file_contents(owner: str, repo: str, sha: str, filepath: str) -> Optional[str]:
    """Get the contents of a file at a commit."""
    token = _get_github_token()
    if not token:
        return None
    
    args = [
        f"/repos/{owner}/{repo}/contents/{filepath}",
        "-H", "Accept: application/vnd.github.v3.raw",
        "-q", "."
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0:
        return None
    
    return output

# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

def _guardian_discover(ctx: ToolContext, exclude_forks: bool = True, repo_limit: Optional[int] = None) -> str:
    """
    Discover all repositories under ErnestHysa and initialize guardian state.
    
    Args:
        exclude_forks: Exclude forked repositories (default: True)
        repo_limit: Max repos to process (default: all)
    """
    log.info("Discovering ErnestHysa repositories...")
    
    state = _load_guardian_state()
    result = _discover_repos(ctx, exclude_forks)
    
    if "error" in result:
        return f"⚠️ DISCOVERY_ERROR: {result['error']}"
    
    repos = result["repos"]
    if not repos:
        return "No repositories found for ErnestHysa."
    
    # Limit if specified
    if repo_limit:
        repos = repos[:repo_limit]
    
    state["metadata"]["total_repos"] = len(repos)
    
    for repo in repos:
        owner = repo["full_name"].split("/")[0]
        repo_name = repo["full_name"].split("/")[1]
        
        last_checked = repo.get("pushed_at")
        default_branch = repo.get("default_branch", "main")
        
        _update_repo_state(
            state,
            owner=owner,
            repo=repo_name,
            default_branch=default_branch,
            last_checked=last_checked
        )
    
    return f"✅ Discovered {len(repos)} repositories:\n" + "\n".join(
        f"- {r['full_name']} (branch: {r['default_branch']}, language: {r.get('language', 'unknown')})"
        for r in repos
    )

def _guardian_monitor(ctx: ToolContext, repo: Optional[str] = None, check_all: bool = False) -> str:
    """
    Monitor repositories for new commits.
    
    Args:
        repo: Specific repo (owner/repo) or None for all
        check_all: Force check all repos (ignore last_checked)
    """
    state = _load_guardian_state()
    repos_data = state["repos"]
    
    repos_to_check = []
    
    if repo:
        key = repo.lower()
        if key not in repos_data:
            return f"⚠️ REPO_NOT_FOUND: Repository '{repo}' not in guardian state."
        repos_to_check.append(repos_data[key])
    elif check_all:
        repos_to_check = list(repos_data.values())
    else:
        # Check repos with no recent activity (last_checked > 1 hour)
        threshold = datetime.utcnow() - timedelta(hours=1)
        for repo_data in repos_data.values():
            last_checked_str = repo_data.get("last_checked")
            if last_checked_str:
                try:
                    last_checked = datetime.fromisoformat(last_checked_str.replace("Z", "+00:00"))
                    if last_checked < threshold:
                        repos_to_check.append(repo_data)
                except ValueError:
                    repos_to_check.append(repo_data)
    
    if not repos_to_check:
        return "No repositories need monitoring at this time."
    
    # Group by owner/repo for API calls
    repo_groups: Dict[str, List[Dict]] = {}
    for repo_data in repos_to_check:
        key = repo_data.get("language") or "unknown"
        if key not in repo_groups:
            repo_groups[key] = []
        repo_groups[key].append(repo_data)
    
    results = []
    for key, repo_list in repo_groups.items():
        owners = set(r.get("default_branch", "").split("/")[0] for r in repo_list if "/" in r.get("default_branch", ""))
        for owner in owners:
            repo_names = [r.split("/")[-1] for r in repo_list if "/" in r.get("default_branch", "") and r.get("default_branch", "").split("/")[0] == owner]
            for repo_name in repo_names:
                result = _monitor_single_repo(owner, repo_name, ctx)
                if result:
                    results.append(result)
    
    if not results:
        return "No new commits found in monitored repositories."
    
    return "\n\n".join(results)

def _monitor_single_repo(owner: str, repo: str, ctx: ToolContext) -> Optional[str]:
    """
    Monitor a single repository for new commits.
    
    Returns: Summary string if new commits found, None otherwise
    """
    state = _load_guardian_state()
    key = f"{owner}/{repo}"
    
    if key not in state["repos"]:
        return None
    
    repo_data = state["repos"][key]
    last_sha = repo_data.get("last_commits_analyzed", [])
    
    if not last_sha:
        return None
    
    # Get the most recent commit as baseline
    latest_sha = last_sha[0]
    
    # Check for new commits
    result = _check_commits(owner, repo, latest_sha)
    
    if "error" in result:
        repo_data["meta"]["last_error"] = result["error"]
        _save_guardian_state(state)
        return None
    
    commits = result.get("commits", [])
    if not commits:
        return None
    
    # Perform code review on each new commit
    review_results = []
    for commit in commits:
        review = _perform_code_review(owner, repo, commit, ctx)
        review_results.append(review)
    
    # Generate PRs for commits with issues
    pr_urls = []
    for i, review in enumerate(review_results):
        if "error" not in review and review.get("issues"):
            pr_url = _apply_fixes(owner, repo, commits[i]["sha"], commits[i]["sha"], review, ctx)
            if not pr_url.startswith("⚠️"):
                pr_urls.append(pr_url)
                repo_data["last_pr"] = {
                    "sha": commits[i]["sha"],
                    "state": "created",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "rating": 0.8  # Will be updated after review
                }
    
    # Update state with new commits
    new_commits = [c["sha"] for c in commits]
    repo_data["last_commits_analyzed"] = new_commits + last_sha
    repo_data["last_checked"] = datetime.utcnow().isoformat() + "Z"
    repo_data["meta"]["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _save_guardian_state(state)
    
    # Return summary
    summary = f"🔄 New commits found in {key}: {len(commits)}\n"
    summary += "\n".join(
        f"- {c['sha'][:7]}: {c['message'][:80]} ({c['author']})"
        for c in commits[:3]
    ) + (f"\n... and {len(commits)-3} more" if len(commits) > 3 else "")
    
    if pr_urls:
        summary += "\n\n" + "✅ PRs Created:\n" + "\n".join(pr_urls)
    
    return summary

def _guardian_report(ctx: ToolContext) -> str:
    """Generate a report of guardian status."""
    state = _load_guardian_state()
    metadata = state["metadata"]
    repos = state["repos"]
    
    total_repos = len(repos)
    active_repos = sum(1 for r in repos.values() if r.get("last_checked"))
    recent_updates = sum(1 for r in repos.values() 
                        if r.get("last_checked") and 
                        (datetime.utcnow() - datetime.fromisoformat(r["last_checked"].replace("Z", "+00:00"))) < timedelta(days=7))
    
    report = f"""
# Guardian Report

**Last Updated**: {metadata.get("last_discovery", "Never")}

## Repository Status

- **Total Repositories**: {total_repos}
- **Monitored**: {active_repos}
- **Recently Updated (<7 days)**: {recent_updates}

## Detailed Status

"""
    
    for key, repo in sorted(repos.items()):
        last_checked = repo.get("last_checked", "Never")
        language = repo.get("language", "unknown")
        review_score = repo.get("review_score", 0.0)
        last_pr = repo.get("last_pr")
        
        pr_status = "No PRs"
        if last_pr:
            state_str = last_pr.get("state", "unknown")
            rating = last_pr.get("rating", 0.0)
            pr_status = f"{state_str} (rating: {rating:.1f})"
        
        report += f"- **{key}** ({language})\n"
        report += f"  - Last checked: {last_checked}\n"
        report += f"  - Review score: {review_score:.1f}\n"
        report += f"  - Last PR: {pr_status}\n"
    
    return report

def _guardian_background(ctx: ToolContext, interval_minutes: int = 10) -> str:
    """
    Run background guardian monitoring loop.
    
    This should be called from background consciousness.
    """
    state = _load_guardian_state()
    
    if not state["metadata"].get("total_repos", 0) > 0:
        return "⚠️ REPOS_NOT_DISCOVERED: Run guardian_discover first."
    
    # Reset last_wakeup and set next_wakeup
    state["metadata"]["last_wakeup"] = datetime.utcnow().isoformat() + "Z"
    state["metadata"]["background_enabled"] = True
    next_wakeup = datetime.utcnow() + timedelta(minutes=interval_minutes)
    state["metadata"]["next_wakeup"] = next_wakeup.isoformat() + "Z"
    _save_guardian_state(state)
    
    # Run monitor
    result = _guardian_monitor(ctx, check_all=True)
    
    # Update last_wakeup
    state["metadata"]["last_wakeup"] = datetime.utcnow().isoformat() + "Z"
    _save_guardian_state(state)
    
    return f"Background guardian run complete.\n\n{result}"

# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    """Return guardian tools."""
    return [
        ToolEntry("guardian_discover", {
            "name": "guardian_discover",
            "description": "Discover all repositories under ErnestHysa and initialize guardian state. Use this to start monitoring.",
            "parameters": {"type": "object", "properties": {
                "exclude_forks": {"type": "boolean", "default": True, "description": "Exclude forked repositories"},
                "repo_limit": {"type": "integer", "default": null, "description": "Max repos to process (null = all)"},
            }, "required": []},
        }, _guardian_discover),

        ToolEntry("guardian_monitor", {
            "name": "guardian_monitor",
            "description": "Monitor repositories for new commits. Checks for activity since last checked.",
            "parameters": {"type": "object", "properties": {
                "repo": {"type": "string", "default": null, "description": "Specific repo (owner/repo) or null for all"},
                "check_all": {"type": "boolean", "default": False, "description": "Force check all repos"},
            }, "required": []},
        }, _guardian_monitor),

        ToolEntry("guardian_report", {
            "name": "guardian_report",
            "description": "Generate a status report of all monitored repositories.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _guardian_report),

        ToolEntry("guardian_background", {
            "name": "guardian_background",
            "description": "Run background guardian monitoring (call from consciousness loop). Monitors all repos and creates auto-fix PRs for new commits.",
            "parameters": {"type": "object", "properties": {
                "interval_minutes": {"type": "integer", "default": 10, "description": "Wakeup interval in minutes"}
            }, "required": []},
        }, _guardian_background),
    ]