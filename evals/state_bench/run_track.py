"""Run the upstream STATE-Bench agent track from Somni's evaluation directory."""

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scorer-model", help="use an OpenAI simulator and judge model for unofficial runs")
    parser.add_argument("benchmark_args", nargs=argparse.REMAINDER, help="run_batch arguments after --")
    options = parser.parse_args()
    arguments = options.benchmark_args
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]

    from state_bench.protocol import EvaluationProtocol
    from state_bench.client import LLMClient
    from state_bench.scripts import run_batch

    with ExitStack() as restore:
        def replace(object_, name, value):
            restore.callback(setattr, object_, name, getattr(object_, name))
            setattr(object_, name, value)

        if options.scorer_model:
            scorer = options.scorer_model
            override = f"simulator+judge={scorer} (unofficial STATE-Bench scorer override)"
            simulator = EvaluationProtocol.simulator_metadata
            judge = EvaluationProtocol.judge_metadata

            def make_scorer():
                return LLMClient(provider="openai", deployment=scorer, api_key_var="OPENAI_API_KEY")

            replace(run_batch, "build_user_sim_client", make_scorer)
            replace(run_batch, "build_locked_judge_client", make_scorer)
            replace(EvaluationProtocol, "simulator_metadata", lambda self, domain: {
                **simulator(self, domain), "simulator_model": scorer, "scorer_override": override,
            })
            replace(EvaluationProtocol, "judge_metadata", lambda self, domain: {
                **judge(self, domain), "judge_model": scorer, "scorer_override": override,
            })

        restore.callback(os.chdir, Path.cwd())
        os.chdir(Path(__file__).resolve().parent)
        replace(sys, "argv", ["run_batch", *arguments])
        return run_batch.main()


if __name__ == "__main__":
    main()
