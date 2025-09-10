from __future__ import annotations

import time
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from activities import (
        analyze_repo_state,
        extract_event_facts,
        notify_slack_on_pr_status,
        publish_portal,
        render_docs,
        update_graph,
    )
    from metrics import (
        record_codex_event,
        record_workflow_completion,
        record_workflow_failure,
        record_workflow_start,
    )


@workflow.defn
class CodexWorkflow:
    @workflow.run
    async def run(self, event: dict[str, Any]):
        workflow_type = "CodexWorkflow"
        start_time = time.time()

        # Record workflow start
        record_workflow_start(workflow_type)

        try:
            # Workflow versioning for future breaking changes
            v = workflow.get_version("codex.analysis", workflow.DEFAULT_VERSION, 2)

            # Record event processing
            event_type = event.get("event", "unknown")

            facts = await workflow.execute_activity(
                extract_event_facts, event, start_to_close_timeout=60
            )
            if facts.get("kind") == "unknown":
                record_codex_event(event_type, success=False)
                duration = time.time() - start_time
                record_workflow_completion(workflow_type, duration, success=False)
                return {"skipped": True, "reason": "unknown_event"}

            # analyze only when PR-like
            if facts["kind"] == "PR":
                analysis = await workflow.execute_activity(
                    analyze_repo_state, facts["repo"], facts["sha"], start_to_close_timeout=600
                )
            else:
                analysis = {}

            await workflow.execute_activity(
                update_graph, facts, analysis, start_to_close_timeout=60
            )
            path = await workflow.execute_activity(
                render_docs, facts, analysis, start_to_close_timeout=120
            )
            urls = await workflow.execute_activity(publish_portal, path, start_to_close_timeout=180)

            # Send Slack notification
            await workflow.execute_activity(
                notify_slack_on_pr_status,
                facts,
                "processed",
                start_to_close_timeout=60,
            )

            # Branch logic by version when semantics change in the future
            # if v >= 2:
            #     # Future enhanced workflow logic
            #     pass

            # Record successful completion
            record_codex_event(event_type, success=True)
            duration = time.time() - start_time
            record_workflow_completion(workflow_type, duration, success=True)

            return {"ok": True, **urls}

        except Exception as e:
            # Record workflow failure
            record_workflow_failure(workflow_type, type(e).__name__)
            record_codex_event(event.get("event", "unknown"), success=False)
            duration = time.time() - start_time
            record_workflow_completion(workflow_type, duration, success=False)
            raise
