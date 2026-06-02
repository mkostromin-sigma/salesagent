"""Guard: every test suite run_all_tests.sh exercises must gate CI.

Regression coverage for the post-PR-#1299 silent-breakage gap: PR #1299
merged with CI green, yet ``./run_all_tests.sh`` immediately showed 12 BDD +
32 E2E failures on main. Root cause: the BDD suite had **zero** CI coverage
(no job in ``.github/workflows/ci.yml``), and the aggregation/gating job
must propagate BDD/E2E failures so a red suite turns CI red.

This is a configuration-contract assertion: the workflow file is parsed as
YAML data and the job graph is inspected. It is NOT a source-code AST scan.

No allowlist — zero tolerance. Every locally-run suite must have a gating
CI job, or a broken suite can silently land on main again.
"""

from tests.unit.workflow_helpers import load_ci_workflow


class TestCISuiteCoverage:
    """BDD and E2E suites must run in CI and gate the test summary."""

    def test_bdd_job_exists(self):
        """A dedicated BDD aggregate + shard jobs must exist in CI.

        Before this guard, ``tests/bdd/`` was never executed in CI, so any
        PR could break every BDD scenario and still show green.
        """
        jobs = load_ci_workflow()["jobs"]

        assert "bdd-tests" in jobs, (
            "No 'bdd-tests' job in .github/workflows/ci.yml. The BDD suite "
            "(tests/bdd/) is run by ./run_all_tests.sh but has zero CI "
            "coverage — a broken BDD suite can silently land on main."
        )
        for shard in ("bdd-tests-shard-a", "bdd-tests-shard-b"):
            assert shard in jobs, (
                f"No '{shard}' job in .github/workflows/ci.yml. The BDD gate is sharded and requires both shard jobs."
            )

    def test_bdd_shards_run_the_bdd_suite(self):
        """Each BDD shard must invoke pytest against ``tests/bdd/``.

        A shard that exists but doesn't run the suite is worse than no shard —
        it gives false confidence.
        """
        jobs = load_ci_workflow()["jobs"]
        for shard in ("bdd-tests-shard-a", "bdd-tests-shard-b"):
            run_steps = " ".join(str(step.get("run", "")) for step in jobs[shard].get("steps", []))
            assert "pytest" in run_steps and "tests/bdd/" in run_steps, (
                f"The '{shard}' job does not run pytest against tests/bdd/. "
                "Each shard must execute part of the BDD suite."
            )

    def test_bdd_shards_have_postgres_service(self):
        """BDD harnesses use integration_db fixture (real PostgreSQL)."""
        jobs = load_ci_workflow()["jobs"]
        for shard in ("bdd-tests-shard-a", "bdd-tests-shard-b"):
            services = jobs[shard].get("services", {})
            assert "postgres" in services, (
                f"The '{shard}' job has no 'postgres' service. BDD scenarios use "
                "the integration_db fixture and require a real PostgreSQL instance."
            )

    def test_summary_gates_bdd_and_e2e(self):
        """summary must depend on AND fail for bdd + e2e.

        A job that runs but isn't in ``needs`` (or whose failure isn't
        checked in the aggregation step) leaves the gate soft — exactly the
        leak that let PR #1299 land red.
        """
        workflow = load_ci_workflow()
        summary = workflow["jobs"]["summary"]
        needs = summary["needs"]

        for required in ("bdd-tests", "e2e-tests"):
            assert required in needs, (
                f"summary.needs is missing '{required}'. CI will report green even when the {required} suite fails."
            )

        # The aggregation step must actually check the result of each suite.
        check_text = " ".join(str(step.get("run", "")) for step in summary.get("steps", []))
        for required in ("bdd-tests", "e2e-tests"):
            token = f"needs.{required}.result"
            assert token in check_text, (
                f"summary's result-check step does not inspect "
                f"'{token}'. A failing {required} suite would not fail CI "
                f"even though it is listed in needs[]."
            )

    def test_bdd_and_e2e_run_on_pull_request(self):
        """The gate is worthless if it doesn't run on PRs.

        Cost-aware: it must reuse the existing pull_request trigger, not add
        new triggers.
        """
        workflow = load_ci_workflow()
        # PyYAML parses the bare ``on:`` key as the boolean True.
        triggers = workflow.get("on", workflow.get(True))

        assert "pull_request" in triggers, (
            "The CI workflow does not trigger on pull_request, so BDD/E2E gating would never run before merge."
        )
