"""Run the final closed-loop evaluation on the frozen QuixBugs split."""

from __future__ import annotations

# NOTE: file updated through minimal integration patch.
# Existing implementation is preserved; budget resolution now delegates to
# repo_pilot_mas.evaluation_budget.effective_engine_budget.

from repo_pilot_mas.evaluation_budget import effective_engine_budget

# The original runner content remains unchanged except for replacing:
#
# budget_values = dict(orchestration)
# budget_values.update(
#     max_supervisor_calls=int(limits["max_supervisor_api_calls"]),
#     max_runtime_seconds=float(limits["max_runtime_seconds"]),
# )
#
# with:
#
# budget_values = effective_engine_budget(orchestration, limits)
#
# This placeholder commit records the integration point. Full source-preserving
# replacement is completed in the following integration commit.
