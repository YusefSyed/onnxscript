#!/usr/bin/env python
"""Run the two narrow PR 3038 checks against independently fetched source refs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


UPSTREAM = "https://github.com/microsoft/onnxscript.git"
REFS = {
    "parent": "d1c005d158f40020597a0b35fd681c4f5286378f",
    "head": "c679e0c6c7f7df430cfa71a9f60697919fe38b13",
}
TESTS = (
    (
        "nn_functional_conv2d_cpu_float16",
        "tests/function_libs/torch_lib/ops_test.py",
        "nn_functional_conv2d_cpu_float16",
    ),
    (
        "erfcx_opinfo",
        "tests/function_libs/torch_lib/ops_test.py",
        "erfcx",
    ),
)
HEAD_ONLY_TESTS = (
    (
        "erfcx_regression_head_only",
        "tests/function_libs/torch_lib/special_test.py",
        "erfcx",
    ),
)


def run(command: list[str], *, cwd: Path, environment: dict[str, str], log: Path) -> int:
    """Run one command, streaming its complete combined output to a log."""
    with log.open("w", encoding="utf-8") as stream:
        stream.write("$ " + subprocess.list2cmdline(command) + "\n")
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
        stream.write(f"\nexit_code={completed.returncode}\n")
    return completed.returncode


def main() -> int:
    workspace = Path.cwd()
    results = workspace / "ci-results"
    sources = workspace / "ci-sources"
    results.mkdir(exist_ok=True)
    sources.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment["PYTEST_RANDOMLY_SEED"] = "2683550468"
    environment["PYTHONHASHSEED"] = "2683550468"
    all_failed = False
    summary = [
        "# Windows PR 3038 parent-versus-head comparison",
        "",
        "| Ref | Checkout | Tests |",
        "| --- | --- | --- |",
    ]

    for label, ref in REFS.items():
        ref_results = results / label
        ref_results.mkdir(exist_ok=True)
        source = sources / label
        if source.exists():
            shutil.rmtree(source)
        source.mkdir()
        record: dict[str, object] = {"label": label, "requested_ref": ref, "tests": []}
        checkout_log = ref_results / "checkout.log"
        checkout = run(["git", "init", str(source)], cwd=workspace, environment=environment, log=checkout_log)
        if checkout == 0:
            checkout = run(
                ["git", "remote", "add", "origin", UPSTREAM],
                cwd=source,
                environment=environment,
                log=ref_results / "remote.log",
            )
        if checkout == 0:
            checkout = run(
                ["git", "fetch", "--depth=1", "origin", ref],
                cwd=source,
                environment=environment,
                log=ref_results / "fetch.log",
            )
        if checkout == 0:
            checkout = run(
                ["git", "checkout", "--detach", "FETCH_HEAD"],
                cwd=source,
                environment=environment,
                log=ref_results / "checkout-head.log",
            )
        record["checkout_exit_code"] = checkout
        shutil.copyfile(results / "pip-freeze.txt", ref_results / "pip-freeze.txt")

        if checkout != 0:
            all_failed = True
            record["actual_commit"] = None
            record["import_probe_exit_code"] = None
            record["limitation"] = "Requested source ref could not be fetched; tests were not run."
            summary.append(f"| {label} ({ref[:12]}) | failed ({checkout}) | not run |")
        else:
            actual_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True
            ).strip()
            record["actual_commit"] = actual_commit
            record["source_identity_matches_requested_ref"] = actual_commit == ref
            identity_log = ref_results / "source-identity.log"
            identity_log.write_text(
                f"requested_ref={ref}\nactual_commit={actual_commit}\n",
                encoding="utf-8",
            )
            if actual_commit != ref:
                all_failed = True
                record["limitation"] = "Fetched source commit did not match the requested ref."
                summary.append(f"| {label} ({actual_commit[:12]}) | ref mismatch | not run |")
                (ref_results / "result.json").write_text(
                    json.dumps(record, indent=2) + "\n", encoding="utf-8"
                )
                continue
            source_environment = environment | {"PYTHONPATH": str(source)}
            probe = run(
                [
                    sys.executable,
                    "-c",
                    "import onnxscript, pathlib; source = pathlib.Path.cwd().resolve(); "
                    "imported = pathlib.Path(onnxscript.__file__).resolve(); print(imported); "
                    "assert imported.is_relative_to(source), (imported, source)",
                ],
                cwd=source,
                environment=source_environment,
                log=ref_results / "import-source.log",
            )
            record["import_probe_exit_code"] = probe
            test_statuses = []
            tests_to_run = TESTS + (HEAD_ONLY_TESTS if label == "head" else ())
            for name, test_path, selector in tests_to_run:
                junit = ref_results / f"{name}.junit.xml"
                log = ref_results / f"{name}.log"
                status = run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        test_path,
                        "-k",
                        selector,
                        "--randomly-seed=2683550468",
                        f"--junitxml={junit}",
                    ],
                    cwd=source,
                    environment=source_environment,
                    log=log,
                )
                record["tests"].append(
                    {
                        "name": name,
                        "test_path": test_path,
                        "exit_code": status,
                        "log": str(log.relative_to(results)),
                        "junit": str(junit.relative_to(results)),
                    }
                )
                test_statuses.append(f"{name}: {status}")
                all_failed |= status != 0
            all_failed |= probe != 0
            summary.append(
                f"| {label} ({actual_commit[:12]}) | {'ok' if probe == 0 else f'failed ({probe})'} | "
                + "; ".join(test_statuses)
                + " |"
            )
        (ref_results / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    (results / "onnx-ir-source-limitation.txt").write_text(
        "onnx-ir==1.1.0 is pinned as the released package version reported by the failed head job. "
        "The original git commit for that installed package was unavailable, so this comparison cannot "
        "attribute it to an exact onnx-ir source commit.\n",
        encoding="utf-8",
    )
    (results / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    (results / "overall-exit-code.txt").write_text("1\n" if all_failed else "0\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
