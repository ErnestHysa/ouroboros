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
    """Run `gh` API command and return (output, returncode)."""
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

def _discover_repos(ctx: ToolContext, exclude_forks: bool = True) -> Dict[str, Any]:
    """
    Discover all repositories under ErnestHysa/* using GitHub API.
    
    Returns: list of repo objects
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
            "-q", ".[] | {name: .name, full_name: .full_name, default_branch: .default_branch, pushed_at: .pushed_at, language: .language, visibility: .visibility, archived: .archived}"
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
    
    return {"repos": repos, "page": page}

def _check_commits(owner: str, repo: str, sha: str) -> Dict[str, Any]:
    """
    Check for commits since a given SHA.
    
    Returns: list of commits after sha
    """
    token = _get_github_token()
    if not token:
        return {"error": "GITHUB_TOKEN not set"}
    
    args = [
        f"/repos/{owner}/{repo}/commits/{sha}",
        "-q", ".commit.authored_date"
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0:
        return {"error": f"Failed to get commit {sha}: {output[:100]}"}
    
    try:
        authored_date = datetime.fromisoformat(output)
    except ValueError:
        return {"error": f"Invalid date format: {output[:100]}"}
    
    # Now find commits since then
    args = [
        f"/repos/{owner}/{repo}/commits",
        f"?since={authored_date.isoformat()}",
        "-q", ".[] | select(.commit.message | ascii_upcase contains('NO_CI') | not) | {sha: .sha, message: .commit.message, date: .commit.authored_date, author: .commit.author.name, url: .html_url}"
    ]
    
    output, rc = _gh_api(args, None)
    
    if rc != 0:
        return {"error": f"Failed to list commits: {output[:100]}"}
    
    try:
        commits = json.loads(output)
    except json.JSONDecodeError:
        return {"error": f"Failed to parse commits JSON: {output[:500]}"}
    
    # Filter commits with NO_CI prefix
    active_commits = [c for c in commits if "NO_CI" not in c["message"].upper()]
    
    return {"commits": active_commits}

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
                result = _monitor_single_repo(owner, repo_name)
                if result:
                    results.append(result)
    
    if not results:
        return "No new commits found in monitored repositories."
    
    return "\n\n".join(results)

def _monitor_single_repo(owner: str, repo: str) -> Optional[str]:
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
    
    # Update state with new commits
    new_commits = [c["sha"] for c in commits]
    repo_data["last_commits_analyzed"] = new_commits + last_sha
    repo_data["last_checked"] = datetime.utcnow().isoformat() + "Z"
    repo_data["meta"]["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _save_guardian_state(state)
    
    # Return summary
    return f"🔄 New commits found in {key}:\n" + "\n".join(
        f"- {c['sha'][:7]}: {c['message'][:80]} ({c['author']})"
        for c in commits[:5]  # Limit to 5
    ) + (f"\n... and {len(commits)-5} more" if len(commits) > 5 else "")

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
    ]