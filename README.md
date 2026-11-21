# Blast Radius

A Docker submission for the [RWX Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). It selects pytest node IDs for independent patches to SQLGlot pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## Run the official sample

```sh
make fetch-kit
make sample
```

The default target builds for `linux/amd64`, the scorer's platform, and uses the scorer's read-only filesystem, memory, CPU, process and network limits. For a native check on Apple Silicon, use `make sample PLATFORM=linux/arm64`. The organizer's amd64 DuckDB wheel can fail to load under local emulation.

If you already downloaded the starter kit, use `make sample KIT=/absolute/path/to/unpacked-kit`. The scorer reads the sample answers outside the container. To inspect the output, open `output.jsonl`.

## Local sample result

The official scorer found **zero misses on all 30 public sample patches** in each of three native arm64 sandbox runs. The simulated score was **100.680 s**, compared with **1,210.470 s** for the starter on the same sample. Including measured selector and child-process CPU, the three-run median was **103.1 s** after rounding as the contest does.

| Native run | Simulated seconds | CPU seconds | Total seconds |
| --- | ---: | ---: | ---: |
| 1 | 100.680 | 2.444 | 103.124 |
| 2 | 100.680 | 2.339 | 103.019 |
| 3 | 100.680 | 2.379 | 103.059 |

The amd64 image also built and passed the sample with zero misses under local emulation. A DuckDB import fault in that environment triggered a full-suite fallback on one patch, so the emulated run cost 137.497 simulated seconds. The public sample and these local timings do not establish a leaderboard or final score.

## How it selects tests

1. Apply each unified diff independently to the pinned tree. Compare Python ASTs to distinguish a change to executable code, an import-time binding, and an edit that only affects source readers. For fixtures, select tests that actually opened the resource.
2. Use the baseline index to map executable lines and function entry points to test node IDs. The profiling tool follows work submitted to Python process pools, so optimizer and executor coverage includes work done in spawned processes.
3. Where the evidence is sufficient, exclude tests whose observed comparison operands produce the same result before and after an operator change. For literal configuration dictionaries with traceable value flow, use actual lookup keys and dictionary origins to narrow the candidate set. Unknown observations take the conservative path.
4. Use bounded pytest runs to clear affordable candidates and investigate collection-sensitive changes. Keep unresolved tests, failed tests, failed subtests and tests from files that fail to collect. Unsupported patches widen selection.
5. Balance test runtimes across one to eight workers. Compare the resulting makespan plus the 2-second per-worker charge. For two workers, integer subset-sum finds the best split when needed.

The image reads only `/data/tests.json` and `/data/commits/*.patch` as contest input. It prints one JSON record per commit. A test is identified by its current manifest number in the output and by its pytest node ID in the index.

## Rebuild the dependency index

`dependencies.json.gz` is a generated baseline artifact. It contains line and function coverage, resource reads, receiver classes, and observed operand and mapping data from the unmodified pinned checkout. The test manifest supplies node IDs and balancing weights. No sample patches, failure labels, or patch-to-answer tables are used to build it.

After `make build` and `make fetch-kit`, run from the repository root:

```sh
mkdir -p index-build
docker run --rm --user 0:0 --platform linux/amd64 \
  --network none --cpus 4 --memory 8g \
  --volume "$PWD/tools:/tools:ro" \
  --volume "$PWD/.sample-kit/sample/tests.json:/manifest.json:ro" \
  --volume "$PWD/index-build:/results" \
  --entrypoint python blast-radius-entry \
  /tools/build_index.py /opt/sqlglot /manifest.json /results/dependencies.json.gz
```

On Apple Silicon, use the same `PLATFORM=linux/arm64` setting for both the build and the profiling container. The shipped index was built on native Linux arm64 with the organizer's Python image and dependency lock. Tracing covered 1,620 cataloged tests. The memory-sensitive generator test did not pass under instrumentation, so it remains eligible via coverage and is excluded from pass-based pruning.
