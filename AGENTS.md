# Agent Development Guide

A file for [guiding coding agents](https://agents.md/).

## Commands

- **Formatting (Python)**: `ruff format`
- **Linting (Python)**: `ruff check --unsafe-fixes`
- **Type Checking (Python)**: `ty check`
- **Local virtualenv (Python)**: `just ready-py`
- **Environment Loading (system)**: `direnv allow`

## Directory Structure

- Python core code: `src/`
- Shared data directory: `../IO/`

## Environments

- New shells should utilize the automatic initialization via `.envrc` and direnv. Use the `Environment Loading` command above.
- Use `uv` for local virtual environment management for python, use the local virtual environment for python development.

## Code Style

- Python

   - Use the `ruff` formatter and linter, respecting all rules present in the `ruff.toml` config file.
   - Use the `ty` type checker to ensure type safety.
   - Use CamelCase for classes and snake_case for functions and variables.
   - Use explicit error handling with try-except blocks.
   - Fully annotate functions' and methods' input and output signatures.
   - Use google docstring formatting for all functions and methods.

- Tests

   - Use the `pytest` framework and place tests within the `test` directory.
   - Classes suffixed with `Test`
   - Methods and functiouns prefixed with `test_`
