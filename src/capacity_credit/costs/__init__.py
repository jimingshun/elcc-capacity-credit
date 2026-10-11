"""Capacity-parameter cost domain (night-5).

The cost formulas live HERE - a standalone domain module - not in the
adequacy engines and not in the workbench facade. The three
calculation classes are strictly separated:

- fixed_cost: the accounting cost chain (template-exact), never a
  Gross CONE;
- net cone (annual, external Gross first) - delivered by its own
  package;
- gross cone (annual project free-cash-flow investment recovery) -
  delivered by its own package.

Cost RUN RECORDS use the versioned cost-run contract under
project/cost_runs/ (never project/runs/, whose honest listing
validates the formal RunManifest contract; a cost manifest there
would corrupt the reliability run listing).
"""

from capacity_credit.costs.fixed_cost import (
    CostInputError,
    compute_fixed_cost,
)

__all__ = ["CostInputError", "compute_fixed_cost"]
