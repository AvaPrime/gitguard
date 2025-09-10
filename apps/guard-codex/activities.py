from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import time
from typing import Any

import psycopg
from embeddings import embed, store_embedding
from metrics import metrics_activity, record_docs_generation, record_graph_update
from owners_emit import emit_owners_index
from ownership import get_owner_type, normalize_owner_handle, owner_for, parse_codeowners
from policy_explain import render_policy_block
from prometheus_client import Histogram
from psycopg.rows import dict_row
from slugify import slugify
from temporalio import activity

from .slack import send_slack_notification

# Constants for magic numbers
MAX_CHANGED_FILES_DISPLAY = 20
MAX_POLICIES_DISPLAY = 10
MAX_FILENAME_LENGTH = 30
MAX_POLICY_NAME_LENGTH = 20
DEFAULT_VECTOR_DIMENSION = 1536
DEFAULT_CLEANUP_DAYS = 90

# Prometheus metrics
DOC_FRESH = Histogram(
    "codex_docs_freshness_seconds",
    "Event-to-doc write latency",
    buckets=(1, 3, 5, 10, 30, 60, 180, 300, 600),
)

# Secrets redaction patterns
REDACT = [
    (r"AKIA[0-9A-Z]{16}", "‹AWS_KEY_REDACTED›"),
    (r"ghp_[0-9A-Za-z]{36,40}(?![0-9A-Za-z])", "‹GH_TOKEN_REDACTED›"),
    (r"(?:ssh-rsa|ssh-ed25519)\s+[A-Za-z0-9/+]+={0,3}", "‹SSH_KEY_REDACTED›"),
]


def _scrub(text: str) -> str:
    """Redact sensitive information from text content."""
    import re

    for pat, repl in REDACT:
        text = re.sub(pat, repl, text)
    return text


DB_URL = os.getenv("DATABASE_URL")
REPO_ROOT = pathlib.Path(os.getenv("REPO_ROOT", "/workspace/repo")).resolve()
DOCS_DIR = pathlib.Path(os.getenv("CODEX_DOCS_DIR", str(REPO_ROOT / "docs"))).resolve()
SITE_DIR = pathlib.Path(os.getenv("CODEX_PORTAL_SITE_DIR", str(REPO_ROOT / "site"))).resolve()
GITHUB_WEB_BASE = os.getenv("GITHUB_WEB_BASE", "")


def _conn():
    return psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)


def _mermaid(pr_num: int, changed: list[str], policies: list[str]) -> str:
    """Generate Mermaid graph showing PR touches and governance relationships."""
    lines = ["```mermaid", "graph LR", f'  PR["PR #{pr_num}"]']
    for file_idx, file_path in enumerate(
        changed[:MAX_CHANGED_FILES_DISPLAY]
    ):  # cap to keep it readable
        display_name = (
            "..." + file_path[-MAX_FILENAME_LENGTH:]
            if len(file_path) > MAX_FILENAME_LENGTH
            else file_path
        )
        lines.append(f'  PR -->|touches| F{file_idx}["{display_name}"]')
    for policy_idx, policy_name in enumerate((policies or [])[:MAX_POLICIES_DISPLAY]):
        display_name = (
            "..." + policy_name[-MAX_POLICY_NAME_LENGTH:]
            if len(policy_name) > MAX_POLICY_NAME_LENGTH
            else policy_name
        )
        lines.append(f'  PR -->|governed_by| P{policy_idx}["{display_name}"]')
    lines.append("```")
    return "\n".join(lines)


def _ensure_schema():
    here = pathlib.Path(__file__).parent
    with _conn() as c, c.cursor() as cur:
        cur.execute((here / "graph_schema.sql").read_text())


def _check_delivery_seen(delivery_id: str) -> bool:
    """Check if delivery has been processed before."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM codex_seen_deliveries WHERE delivery_id = %s", (delivery_id,))
        return cur.fetchone() is not None


def _mark_delivery_seen(delivery_id: str) -> None:
    """Mark delivery as processed."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO codex_seen_deliveries (delivery_id) VALUES (%s) ON CONFLICT (delivery_id) DO NOTHING",
            (delivery_id,),
        )


def _validate_vector_dimension(
    vector: list[float], expected_dim: int = DEFAULT_VECTOR_DIMENSION
) -> bool:
    """Validate vector dimension matches embedder expectation."""
    return len(vector) == expected_dim


def _cleanup_old_deliveries(days_to_keep: int = DEFAULT_CLEANUP_DAYS) -> None:
    """Clean up old delivery records beyond retention period."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "DELETE FROM codex_seen_deliveries WHERE received_at < now() - interval '%s days'",
            (days_to_keep,),
        )


def _insert_embedding(
    cur, node_id: str, vector: list[float], model: str = "text-embedding-3-large"
) -> None:
    """Insert embedding with vector dimension validation."""
    if not _validate_vector_dimension(vector):
        raise ValueError(f"Vector dimension {len(vector)} does not match expected dimension 1536")

    cur.execute(
        "INSERT INTO codex_embeddings (node_id, model, vector) VALUES (%s, %s, %s) ON CONFLICT (node_id) DO UPDATE SET model=EXCLUDED.model, vector=EXCLUDED.vector",
        (node_id, model, vector),
    )


def _cleanup_temporary_branch_edges(days_to_keep: int = DEFAULT_CLEANUP_DAYS) -> None:
    """Clean up edges from temporary branches beyond retention period."""
    with _conn() as c, c.cursor() as cur:
        # Remove edges from nodes that represent temporary branches (feature/, hotfix/, etc.)
        cur.execute(
            """
            DELETE FROM codex_edges
            WHERE src IN (
                SELECT id FROM codex_nodes
                WHERE ntype = 'PR'
                AND (data->>'branch_name' LIKE 'feature/%'
                     OR data->>'branch_name' LIKE 'hotfix/%'
                     OR data->>'branch_name' LIKE 'temp/%')
                AND created_at < now() - interval '%s days'
            )
        """,
            (days_to_keep,),
        )

        # Also clean up the temporary branch nodes themselves
        cur.execute(
            """
            DELETE FROM codex_nodes
            WHERE ntype = 'PR'
            AND (data->>'branch_name' LIKE 'feature/%'
                 OR data->>'branch_name' LIKE 'hotfix/%'
                 OR data->>'branch_name' LIKE 'temp/%')
            AND created_at < now() - interval '%s days'
        """,
            (days_to_keep,),
        )


@activity.defn
@metrics_activity
async def extract_event_facts(event: dict[str, Any]) -> dict[str, Any]:
    # Normalize GitHub webhook-ish payloads already routed via Guard API
    # Expected keys: type in {push, pull_request, release, issues}, repo.name, repo.full_name, sender.login, etc.
    e = event

    # Check for delivery deduplication
    delivery_id = e.get("delivery_id") or e.get("id", "unknown")
    if _check_delivery_seen(str(delivery_id)):
        raise ValueError(f"Delivery {delivery_id} already processed")

    # Mark delivery as seen
    _mark_delivery_seen(str(delivery_id))
    repo = e.get("repository", {}).get("full_name") or e.get("repo", "")
    rname = repo.split("/")[-1] if repo else e.get("repo_name", "unknown")
    kind = e.get("event", e.get("type", "unknown"))

    if kind == "pull_request":
        pr = e["pull_request"]
        number = pr["number"]
        head_sha = pr["head"]["sha"]
        # Risk/OPA signals should already be on payload from Guard API; fall back to defaults if missing
        risk = e.get("risk", {}).get("score", 0)
        checks_passed = bool(e.get("checks", {}).get("all_passed", False))
        labels = [l["name"] for l in pr.get("labels", [])]
        changed_paths = e.get("changed_files", [])  # Guard API can precompute
        coverage_delta = e.get("coverage_delta", 0.0)
        perf_delta = e.get("perf_delta", 0.0)
        release_window_state = e.get("release_window_state", "unknown")

        summary = e.get("summary") or pr.get("body") or ""
        data = {
            "kind": "PR",
            "repo": repo,
            "repo_name": rname,
            "number": number,
            "sha": head_sha,
            "title": pr.get("title", ""),
            "author": pr.get("user", {}).get("login", "unknown"),
            "risk_score": risk,
            "checks_passed": checks_passed,
            "labels": labels,
            "changed_paths": changed_paths,
            "coverage_delta": coverage_delta,
            "perf_delta": perf_delta,
            "release_window_state": release_window_state,
            "policies": e.get("policies", []),
            "adrs": e.get("adrs", []),
            "summary": summary.strip(),
        }
        return data

    if kind == "release":
        rel = e["release"]
        return {
            "kind": "Release",
            "repo": repo,
            "repo_name": rname,
            "tag": rel["tag_name"],
            "title": rel.get("name") or rel["tag_name"],
            "body": rel.get("body", ""),
            "author": rel.get("author", {}).get("login", "unknown"),
            "created_at": rel.get("created_at"),
        }

    # Fallback
    return {"kind": kind, "repo": repo, "repo_name": rname, "raw": e}


@activity.defn
@metrics_activity
async def analyze_repo_state(repo: str, sha: str) -> dict[str, Any]:
    # Minimal analysis; safe to run even without tree-sitter
    # You can extend with real AST, coverage/perf harvesters.
    repo_dir = REPO_ROOT

    # changed files list can be computed from git if not provided
    def git(args: list[str]) -> str:
        return subprocess.check_output(["git", "-C", str(repo_dir), *args]).decode()

    try:
        base = git(["merge-base", "origin/main", sha]).strip()
        paths = git(["diff", "--name-only", f"{base}...{sha}"]).splitlines()
    except Exception:
        paths = []

    # Symbol detection: naive map for Python modules
    py_files = [p for p in paths if p.endswith(".py")]
    symbols = [{"path": p, "name": pathlib.Path(p).stem} for p in py_files]

    # Parse CODEOWNERS and determine file ownership (if owners feature is enabled)
    owners = {}
    if os.getenv("CODEX_OWNERS_ENABLED", "false").lower() == "true":
        co = REPO_ROOT / ".github" / "CODEOWNERS"
        rules = parse_codeowners(co.read_text()) if co.exists() else []
        owners = {p: owner_for(p, rules) for p in paths}

    return {
        "paths": paths,
        "symbols": symbols,
        "owners": owners,
    }


def _upsert_node(cur, ntype: str, nkey: str, title: str, data: dict[str, Any]) -> str:
    cur.execute(
        """
        INSERT INTO codex_nodes (ntype, nkey, title, data)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (ntype, nkey)
        DO UPDATE SET title=EXCLUDED.title, data=codex_nodes.data || EXCLUDED.data, updated_at=now()
        RETURNING id
    """,
        (ntype, nkey, title, json.dumps(data)),
    )
    return cur.fetchone()["id"]


def _link(cur, src_id: str, dst_id: str, rel: str, data: dict[str, Any] | None = None):
    cur.execute(
        """
        INSERT INTO codex_edges (src, dst, rel, data)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (src, dst, rel) DO NOTHING
    """,
        (src_id, dst_id, rel, json.dumps(data or {})),
    )


@activity.defn
@metrics_activity
async def update_graph(facts: dict[str, Any], analysis: dict[str, Any]) -> None:
    _ensure_schema()
    with _conn() as c, c.cursor() as cur:
        kind = facts["kind"]
        if kind == "PR":
            pr_key = f"pr:{facts['number']}"
            pr_title = facts.get("title") or f"PR #{facts['number']}"
            pr_id = _upsert_node(cur, "PR", pr_key, pr_title, facts)

            repo_id = _upsert_node(
                cur, "Repo", facts["repo"], facts["repo_name"], {"full": facts["repo"]}
            )
            _link(cur, repo_id, pr_id, "has_pr")

            for p in facts["changed_paths"] or analysis.get("paths", []):
                f_id = _upsert_node(cur, "File", f"file:{p}", p, {"path": p})
                _link(cur, pr_id, f_id, "touches")

                # Create ownership relationships (if owners feature is enabled)
                if os.getenv("CODEX_OWNERS_ENABLED", "false").lower() == "true":
                    ownership = analysis.get("owners", {}).get(p)
                    if ownership:
                        pattern, handles = ownership
                        for handle in handles:
                            normalized_handle = normalize_owner_handle(handle)
                            owner_type = get_owner_type(handle)
                            owner_key = f"owner:{normalized_handle}"
                            owner_title = (
                                f"@{normalized_handle}"
                                if owner_type == "user"
                                else f"@{normalized_handle}"
                            )
                            owner_data = {
                                "handle": normalized_handle,
                                "type": owner_type,
                                "original_handle": handle,
                            }
                            owner_id = _upsert_node(
                                cur, "Owner", owner_key, owner_title, owner_data
                            )
                            _link(cur, owner_id, f_id, "owns", {"pattern": pattern})

            for s in analysis.get("symbols", []):
                skey = f"symbol:{s['path']}#{s['name']}"
                s_id = _upsert_node(cur, "Symbol", skey, s["name"], s)
                _link(cur, pr_id, s_id, "defines")

            for policy in facts.get("policies", []):
                pol_id = _upsert_node(cur, "Policy", f"policy:{policy}", policy, {})
                _link(cur, pr_id, pol_id, "governed_by")

            for adr in facts.get("adrs", []):
                adr_id = _upsert_node(cur, "ADR", f"adr:{adr}", adr, {})
                _link(cur, pr_id, adr_id, "governed_by")

            # Record graph update metrics
            record_graph_update("pr_update")

        elif kind == "Release":
            rel_key = f"release:{facts['tag']}"
            rel_id = _upsert_node(cur, "Release", rel_key, facts["title"], facts)
            repo_id = _upsert_node(
                cur, "Repo", facts["repo"], facts["repo_name"], {"full": facts["repo"]}
            )
            _link(cur, repo_id, rel_id, "has_release")

            # Record graph update metrics
            record_graph_update("release_update")


@activity.defn
@metrics_activity
async def render_docs(facts: dict[str, Any], analysis: dict[str, Any]) -> str:
    start = time.time()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    # Ensure base sections
    (DOCS_DIR / "prs").mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "releases").mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.md").write_text("# GitGuard Codex\n\nWelcome to the living org-brain.\n")

    if facts["kind"] == "PR":
        pr = facts
        p = DOCS_DIR / "prs" / f"{pr['number']}.md"
        gh_link = f"{GITHUB_WEB_BASE}/pull/{pr['number']}" if GITHUB_WEB_BASE else ""
        changed = pr.get("changed_paths") or analysis.get("paths", [])

        # Generate policy explanation block
        policy_md = render_policy_block(
            pr.get("policies", []), pr.get("opa_input", {}), str(REPO_ROOT / "policies")
        )

        # Generate Mermaid graph
        merm = _mermaid(pr["number"], changed, pr.get("policies", []))

        body = f"""# PR #{pr['number']}: {pr['title']}

**Risk:** {pr['risk_score']} • **Checks:** {"✓" if pr['checks_passed'] else "✗"}
**Coverage Δ:** {pr['coverage_delta']}% • **Perf Δ:** {pr['perf_delta']} • **Labels:** {", ".join(pr['labels']) or "-"}
**Release window:** {pr['release_window_state']}{f" • [GitHub]({gh_link})" if gh_link else ""}

## Summary
{pr['summary'] or "_No summary provided._"}

## Changed Files
{chr(10).join(f"- `{f}`" for f in changed) or "_none_"}

## Governance
- Policies: {", ".join(pr.get('policies', [])) or "—"}
- ADRs impacted: {", ".join(pr.get('adrs', [])) or "—"}

### Graph
{merm}

{policy_md}
"""
        p.write_text(_scrub(body))
        record_docs_generation("pr_doc")

    if facts["kind"] == "Release":
        rel = facts
        p = DOCS_DIR / "releases" / f"{slugify(rel['tag'])}.md"
        gh_link = f"{GITHUB_WEB_BASE}/releases/tag/{rel['tag']}" if GITHUB_WEB_BASE else ""
        release_body = f"""# Release {rel['tag']}

**Author:** {rel['author']} • **Created:** {rel.get('created_at') or dt.datetime.utcnow().isoformat()}
{f"[GitHub]({gh_link})" if gh_link else ""}

## Notes
{rel.get('body') or "_No notes provided._"}
"""
        p.write_text(_scrub(release_body))
        record_docs_generation("release_doc")

    # Update owners.md with current ownership data (if enabled)
    if os.getenv("CODEX_OWNERS_ENABLED", "false").lower() == "true":
        _update_owners_doc()

    # Generate and store embeddings for PR content (if enabled)
    if os.getenv("CODEX_EMBEDDINGS_ENABLED", "false").lower() == "true":
        _generate_embeddings(facts)

    # Update owners index from graph data
    if facts["kind"] in ["PR", "Release"]:
        emit_owners_index(DB_URL, str(DOCS_DIR))

    # Record freshness metric
    DOC_FRESH.observe(time.time() - start)

    return str(DOCS_DIR)


def _generate_embeddings(facts: dict[str, Any]) -> None:
    """
    Generate and store embeddings for PR summaries and titles.

    Args:
        facts: The facts dictionary containing PR or Release information
    """
    try:
        if facts["kind"] == "PR":
            pr = facts
            pr_number = pr.get("number")
            if not pr_number:
                return

            # Create embedding text from title and summary
            title = pr.get("title", "")
            summary = pr.get("summary", "")

            if not title and not summary:
                return

            # Combine title and summary for embedding
            embedding_text = f"{title}\n\n{summary}".strip()

            # Generate embedding
            embedding_vector = embed(embedding_text)
            if not embedding_vector:
                # Skip if embedding generation failed or is unavailable
                return

            # Find the PR node in the database to get its ID
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT id FROM codex_nodes WHERE ntype = 'PR' AND nkey = %s",
                    (f"pr:{pr_number}",),
                )
                result = cur.fetchone()

                if result:
                    node_id = result["id"]
                    # Store the embedding
                    success = store_embedding(str(node_id), embedding_vector)
                    if success:
                        record_docs_generation("pr_embedding")

        elif facts["kind"] == "Release":
            rel = facts
            tag = rel.get("tag")
            if not tag:
                return

            # Create embedding text from tag and body
            body = rel.get("body", "")

            if not body:
                return

            # Use release notes for embedding
            embedding_text = f"Release {tag}\n\n{body}".strip()

            # Generate embedding
            embedding_vector = embed(embedding_text)
            if not embedding_vector:
                return

            # Find the Release node in the database
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT id FROM codex_nodes WHERE ntype = 'Release' AND nkey = %s",
                    (f"release:{tag}",),
                )
                result = cur.fetchone()

                if result:
                    node_id = result["id"]
                    # Store the embedding
                    success = store_embedding(str(node_id), embedding_vector)
                    if success:
                        record_docs_generation("release_embedding")

    except Exception as e:
        # Log error but don't fail the entire render process
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Failed to generate embeddings: {e}")


def _update_owners_doc():
    """Update the owners.md file with current ownership data from the graph."""
    # Only proceed if owners feature is enabled
    if os.getenv("CODEX_OWNERS_ENABLED", "false").lower() != "true":
        return

    with _conn() as c, c.cursor() as cur:
        # Query for owners and their file counts
        cur.execute(
            """
            SELECT
                o.title as owner_name,
                o.data->>'type' as owner_type,
                o.data->>'handle' as handle,
                COUNT(DISTINCT e.dst) as file_count,
                MAX(f.updated_at) as last_activity
            FROM codex_nodes o
            JOIN codex_edges e ON o.id = e.src AND e.rel = 'owns'
            JOIN codex_nodes f ON e.dst = f.id AND f.ntype = 'File'
            WHERE o.ntype = 'Owner'
            GROUP BY o.id, o.title, o.data->>'type', o.data->>'handle'
            ORDER BY file_count DESC, o.title
        """
        )

        owners_data = cur.fetchall()

        if not owners_data:
            return  # No ownership data yet

        # Generate the table content
        table_rows = []
        for owner in owners_data:
            owner_name = owner["owner_name"]
            owner_type = owner["owner_type"] or "unknown"
            file_count = owner["file_count"]
            last_activity = owner["last_activity"]

            # Format last activity
            if last_activity:
                activity_str = last_activity.strftime("%Y-%m-%d")
            else:
                activity_str = "No recent activity"

            # Format owner type
            type_display = owner_type.title()

            table_rows.append(f"| {owner_name} | {type_display} | {file_count} | {activity_str} |")

        # Read current owners.md content
        owners_file = DOCS_DIR / "owners.md"
        if owners_file.exists():
            content = owners_file.read_text()

            # Find the table section and replace it
            table_start = content.find("| Owner | Type | Files Owned | Recent Activity |")
            if table_start != -1:
                # Find the end of the table (next section or end of content)
                table_end = content.find("\n\n*This table is automatically updated", table_start)
                if table_end != -1:
                    table_end = (
                        content.find("*", table_end)
                        + content[table_end:].find("\n")
                        + table_end
                        + 1
                    )

                    # Replace the table content
                    new_table = "| Owner | Type | Files Owned | Recent Activity |\n"
                    new_table += "|-------|------|-------------|----------------|\n"
                    new_table += "\n".join(table_rows) + "\n\n"
                    new_table += "*This table is automatically updated based on CODEOWNERS patterns and file changes in pull requests.*"

                    updated_content = content[:table_start] + new_table + content[table_end:]
                    owners_file.write_text(updated_content)
                    record_docs_generation("owners_doc")


@activity.defn
@metrics_activity
async def publish_portal(path: str) -> dict[str, str]:
    # Build MkDocs locally; CI can deploy to Pages or S3.
    site = SITE_DIR
    site.mkdir(parents=True, exist_ok=True)
    # Find repo root (mkdocs.yml should live there)
    repo_root = REPO_ROOT
    subprocess.check_call(
        ["python", "-m", "mkdocs", "build", "--clean", "--site-dir", str(site)], cwd=str(repo_root)
    )
    record_docs_generation("portal_publish")
    return {"site_dir": str(site)}


@activity.defn
@metrics_activity
async def notify_slack_on_pr_status(facts: dict[str, Any], status: str) -> None:
    """Sends a Slack notification about a PR status change."""
    if facts.get("kind") != "PR":
        return  # Only send notifications for PRs

    pr_number = facts.get("number")
    if not pr_number:
        return

    # Construct PR URL
    gh_web_base = os.getenv("GITHUB_WEB_BASE", "")
    pr_url = f"{gh_web_base}/pull/{pr_number}" if gh_web_base else ""

    send_slack_notification(
        pr_url=pr_url,
        pr_title=facts.get("title", ""),
        pr_number=pr_number,
        status=status,
        author=facts.get("author", "unknown"),
        changed_files=facts.get("changed_paths", []),
    )


@activity.defn
@metrics_activity
async def cleanup_database() -> dict[str, Any]:
    """Periodic cleanup of old delivery records and temporary branch data."""
    try:
        # Clean up old delivery records (90 days)
        _cleanup_old_deliveries(90)

        # Clean up temporary branch edges (90 days)
        _cleanup_temporary_branch_edges(90)

        return {
            "status": "success",
            "cleaned_deliveries": True,
            "cleaned_temp_branches": True,
            "retention_days": 90,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
