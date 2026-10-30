import pytest

from idemrun import Context, Executor, Host, parse_spec, plan_fleet


@pytest.fixture
def ctx(tmp_path):
    return Context("h1", str(tmp_path))


def converge(data, hosts, check=False, jobs=4, diff=False):
    """Plan and run a spec given as a dict against a list of Hosts."""
    plans, roots = plan_fleet(parse_spec(data), hosts)
    return Executor(jobs=jobs, check=check, diff=diff).run(plans, roots)


def statuses(report, host=None):
    return {o.rid: o.status for o in report.outcomes if host is None or o.host == host}


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "h1"
    root.mkdir()
    return Host("h1", str(root))
