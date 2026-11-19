"""Somni learning-track agent for STATE-Bench's runner."""

from state_bench.agents.state_bench import StateBenchAgent

from evals.state_bench.somni_state_bench import SomniWikiSearchLearnings


class SomniWikiSearchAgent(SomniWikiSearchLearnings, StateBenchAgent):
    pass
