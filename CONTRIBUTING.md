# Contributing to agy-multi

Thank you for your interest in contributing to `agy-multi`!

## Code of Conduct

Please be respectful, collaborative, and considerate of others when participating in this project.

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/awu5425/agy-multi.git
   cd agy-multi
   ```

2. **Create a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   pip install pytest
   ```

3. **Run the test suite**:
   ```bash
   PYTHONPATH=. pytest tests/ -v
   ```

## Pull Request Guidelines

1. **Keep PRs Focused**: Each PR should address a single bug fix or feature.
2. **Never Commit Secrets**: Ensure no OAuth client secrets, tokens, API keys, or private environment files are committed.
3. **Add Tests**: Include unit tests under `tests/` for any new functionality or bug fix.
4. **Maintain Compatibility**: Preserve backward compatibility with Python 3.10+ and the standard library design (zero third-party runtime dependencies).
5. **Update Documentation**: Update `README.md`, `README_zh.md`, and `PRD.md` when adding or modifying commands or configuration options.
