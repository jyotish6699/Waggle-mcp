# Contributing

Thanks for your interest in improving Waggle.

## How to contribute

- Open an issue for bugs, docs gaps, benchmark methodology concerns, or feature proposals.
- Open a pull request with a focused change and a clear description of impact.
- For benchmark-claim changes in `README.md`, include updated artifact links under `tests/artifacts/`.

## Local validation

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

If your PR changes benchmark-facing numbers, regenerate the corresponding artifacts and update `tests/artifacts/README.md`.
