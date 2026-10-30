"""idemrun: a small declarative, idempotent configuration engine."""
from .executor import Executor, Outcome, Report
from .planner import Plan, PlanError, build_plan
from .resources import Context, ResourceError
from .spec import Host, SpecError, instantiate, load_inventory, load_spec, parse_spec

__version__ = "0.1.0"


def plan_fleet(spec, hosts):
    """Render and plan `spec` for every host. Returns (plans, roots)."""
    plans = [build_plan(h.name, instantiate(spec, h)) for h in hosts]
    return plans, {h.name: h.root for h in hosts}


__all__ = ["Context", "Executor", "Host", "Outcome", "Plan", "PlanError", "Report",
           "ResourceError", "SpecError", "build_plan", "instantiate", "load_inventory",
           "load_spec", "parse_spec", "plan_fleet"]
