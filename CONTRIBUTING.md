# Contributing

Thanks for considering a contribution — this started as a single-user
personal project, so please keep that spirit in mind: simple, offline-first,
low-dependency.

## Getting set up

1. Fork and clone the repo.
2. Follow the **Setup** section in [README.md](README.md) to install
   dependencies and configure `.env`.
3. Run the test suite before you start and again before opening a PR:
   ```
   python3 test_bot.py
   ```
   All tests mock Telegram/Ollama/Moonshine, so no external services or
   credentials are needed to run them.

## Making changes

- Keep changes scoped — this codebase intentionally avoids abstractions it
  doesn't need yet. A small, focused PR is easier to review than a large one.
- Add or update tests in `test_bot.py` for any behavior change.
- Match the existing style: no type-hint-free public functions, docstrings
  only where the *why* isn't obvious from the code.
- Update `README.md` if you change setup steps, commands, or behavior a user
  would notice.

## Reporting bugs / requesting features

Open a GitHub issue with:
- What you expected vs. what happened.
- Steps to reproduce (a sample voice/text message if relevant).
- Your environment (OS, Python version, Ollama model).

## Pull requests

- One logical change per PR.
- Make sure `python3 test_bot.py` passes.
- Describe *why* the change is needed, not just what it does.
