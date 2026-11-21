# Blast Radius

A Docker submission for the [RWX Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). It selects pytest node IDs for independent patches to SQLGlot pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## Run the official sample

```sh
make fetch-kit
make sample
```

The default target builds for `linux/amd64`, the scorer's platform, and uses the scorer's read-only filesystem, memory, CPU, process and network limits. For a native check on Apple Silicon, use `make sample PLATFORM=linux/arm64`. The organizer's amd64 DuckDB wheel can fail to load under local emulation.

If you already downloaded the starter kit, use `make sample KIT=/absolute/path/to/unpacked-kit`. The scorer reads the sample answers outside the container. To inspect the output, open `output.jsonl`.

## Local validation

The official scorer found **zero misses on all 30 public sample patches** in each of three native arm64 sandbox runs. Simulated time was **99.830 s**. The median including selection CPU was **102.3 s**. The amd64 image built and produced zero misses on the local emulator as well.

| Local set | Previous simulated seconds | Current simulated seconds | Current misses |
| --- | ---: | ---: | ---: |
| Earlier 31 labeled mutations plus the baseline | 247.623 | 191.530 | 0 |
| Independent pytest/JUnit set, 13 runs including the baseline | 274.615 | 137.745 | 0 |
| Three expensive parser cases within the independent set | 73.762 | 8.306 | 0 |

The comparison uses the default runtime policy and excludes CPU from simulated seconds. Independent labels came from pytest JUnit reports with per-test timeouts. This recovered a parser case that the earlier whole-suite timeout had left incomplete. Four follow-up parser stress runs also produced no misses. Hidden competition scores are separate from these local results.

The image accepts `--verification static`, `--verification collection`, or `--verification adaptive` (the default). Collection mode checks imports without pruning tests on the basis of a passing rerun. On the independent set, static, collection, and adaptive all had zero misses. Their measured totals including CPU were 143.5 s, 145.7 s, and 146.1 s, respectively. The default keeps collection checks where static analysis has incomplete evidence and allows budgeted reruns.

## How it selects tests

1. Apply each unified diff independently to the pinned tree. Compare Python ASTs and executable code to separate runtime changes from edits that only affect source readers. For fixtures, select tests that actually opened the resource.
2. Use line and function coverage, including work in spawned processes. Trace resolved imports and cached dialect lookups to find consumers of changed modules. For literals stored on classes, use the actual field binding where the index has complete evidence.
3. Compare recorded baseline values with the patched expression when the operands have supported builtin types. For example, changing the threshold can leave every observed comparison unchanged. The index also identifies actual dictionary value reads, integer indexing, and function arguments bound to Python singletons. A focused parser trace records token and argument observations at match gates. It can rule out effects when both versions miss without moving the cursor. Unknown receiver implementations, compound edits, and missing observations fall back to broader coverage.
4. Collect files that still have unselected tests when an edit might prevent imports. Rerun selected candidates when the expected reduction in worker time justifies the CPU cost. If collection is interrupted, retain tests from unchecked files. Keep unresolved tests, failed tests, failed subtests, and tests from files that fail to collect. Unsupported patches widen selection.
5. Balance test runtimes across one to eight workers. For two workers, integer subset-sum finds the best split when beneficial. Tests with weaker dependency evidence can fill unused worker capacity when they add no simulated seconds.

The image reads only `/data/tests.json` and `/data/commits/*.patch` as contest input. It prints one JSON record per commit. A test is identified by its current manifest number in the output and by its pytest node ID in the index.

## Rebuild the dependency index

`dependencies.json.gz` is a generated baseline artifact. It contains line and function coverage, resource reads, resolved imports, receiver classes, and observed operands, arguments, mapping lookups, and parser token states from the unmodified pinned checkout. The test manifest supplies node IDs and balancing weights. No sample patches, failure labels, or patch-to-answer tables are used to build it.

After `make build` and `make fetch-kit`, run from the repository root:

```sh
mkdir -p index-build
docker run --rm --user 0:0 --platform linux/amd64 \
  --network none --cpus 4 --memory 8g \
  --volume "$PWD:/entry:ro" \
  --volume "$PWD/.sample-kit/sample/tests.json:/manifest.json:ro" \
  --volume "$PWD/index-build:/results" \
  --entrypoint python blast-radius-entry \
  /entry/tools/build_index.py /opt/sqlglot /manifest.json /results/dependencies.json.gz
```

To refresh only the parser observations, append `--extend-guards /entry/dependencies.json.gz` to the profiling command. On Apple Silicon, use the same `PLATFORM=linux/arm64` setting for both the build and the profiling container. The shipped index combines two baseline runs with different hash seeds on native Linux arm64 using the organizer's Python image and dependency lock. Tracing covered 1,620 cataloged tests. The memory-sensitive generator test did not pass under instrumentation, so it remains eligible via coverage and is excluded from pass-based pruning.
