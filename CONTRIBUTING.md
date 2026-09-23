# Contributing to Hindsight

Thanks for your interest in contributing to Hindsight!

## Fork checks

Clone `0xble/hindsight`, then run from that checkout:

```sh
./bin/ci
```

Prerequisites: Git, Python 3.9+ for bootstrap, uv 0.9.28, Go 1.27.1, and executable
`kill` and `ps` on PATH (`procps` on Debian/Ubuntu). Setup and checks reject a
different uv version before using the prepared build cache. Setup selects the
repository's Python 3.11, installs locked dependencies
and downloads two pinned public models. It needs internet access, disk space for
the ML environment and no credentials. PostgreSQL is bundled in the locked pg0
wheel. Docker and an existing database are not required.

The complete command performs setup followed by checks. For repeated edits:

```sh
./bin/ci setup   # repeat after dependency or model pins change
./bin/ci check   # checks do not rewrite tracked source
./bin/ci list    # show maintained coverage
```

Checks run without provider credentials or repository `.env` loading. Each run
owns disk-backed scratch under its checkout's `.ci/runs`, a private HOME and a
PostgreSQL data directory, binds a separate loopback
port and stops its server on completion, failure or cancellation. Real CPU
embeddings and reranking use verified local model files in offline mode. A
missing dependency, changed model or skipped maintained test fails the gate.
Failed-run diagnostics are retained at the path printed by the command.

Every linked worktree has its own `.venv` and `.ci` inputs. Run setup in each
worktree before checking it. Independent worktrees may run checks concurrently.
Do not run setup while another command is using the same worktree environment.
Neither command changes Git hook configuration or reads dependencies from an
ancestor checkout. Delete only that checkout's ignored `.ci` and `.venv` if a
fresh setup is needed after all of its checks have stopped.

The maintained gate covers API lint, format, types, build and imports, the 26
retained-patch/upstream-recovery test files, Python and Go operation-detail
regressions, and CI lifecycle/policy tests. It does not run the full upstream
provider, UI, integration or benchmark matrix. Add relevant checks when extending
those surfaces. The complete gate is qualified natively on macOS ARM64. Linux
runner qualification is separate. The pg0 wheel requires glibc 2.35+ on Linux,
macOS 14+ on ARM64, or macOS 15+ on Intel. This POSIX entrypoint does not claim
Windows qualification. The former manual Windows OCR/import smoke and performance
workflow are not part of its green result. Use the existing OCR tests on a native
Windows environment and `scripts/benchmarks/run-perf-test.sh` in an explicitly
prepared benchmark environment when that coverage is needed.

GitHub Actions are disabled in this fork. A trusted standalone runner can execute
the same entrypoint and publish the required check for the exact PR commit.
Merge through normal GitHub PR controls. Contributors need no personal tools,
review receipts, JSON command catalog or custom merge command. Runner enrollment
and enforcement are maintained separately from this executable.

## Application development

`./scripts/dev/setup.sh` prepares the broader interactive application environment,
including Node/Rust clients and optional provider configuration. It is separate
from the maintained gate. Run it only when developing those application surfaces.

### Manual setup

If you'd rather set things up by hand instead of running the script above:

1. Set up your environment:
   ```bash
   cp .env.example .env
   ```
   Edit the .env to add LLM API key and config as required

2. Install dependencies:
   ```bash
   # Python dependencies
   uv sync --directory hindsight-api/

   # Node dependencies (uses npm workspaces)
   npm install
   ```

## Development

### Running the API locally

```bash
./scripts/dev/start-api.sh
```

### Running the Control Plane locally

```bash
./scripts/dev/start-control-plane.sh
```

### Running the documentation locally

```bash
./scripts/dev/start-docs.sh
```

### Running tests

Run `./bin/ci check` for the maintained fork gate. Broader tests in
`hindsight-api-slim/tests` may require explicitly configured providers or services.

### Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for Python linting and formatting, and ESLint/Prettier for TypeScript.

#### Optional git hooks

For a single commit, `git -c core.hooksPath=.githooks commit` enables the existing
application formatting hooks without changing shared Git configuration. They
may format source and install broader development dependencies. They are optional
and do not publish CI results or gate merges. `scripts/setup-hooks.sh` installs
these hooks only for the selected worktree when persistent feedback is wanted.

#### Manual linting and formatting

```bash
# Run all lints (same as pre-commit)
./scripts/hooks/lint.sh

# Or run individually for Python:
cd hindsight-api
uv run ruff check --fix .   # Lint and auto-fix
uv run ruff format .        # Format code
uv run ty check hindsight_api  # Type check
```

#### Style guidelines

- Use Python type hints
- Follow existing code patterns
- Keep functions focused and well-named

## Pull Requests

1. Create a feature branch from `main`
2. Make your changes
3. Run tests to ensure nothing breaks
4. Submit a PR with a clear description of changes

## Releases

This fork does not own upstream package publication, signing or deployment.
Retained release scripts are upstream application tooling, not contributor CI.
Do not run them as part of this fork's check or merge process.

## Reporting Issues

Open an issue on GitHub with:
- Clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Python version)

## Questions?

Open a discussion on GitHub or reach out to the maintainers.
