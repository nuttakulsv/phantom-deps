# phantom-deps

[ภาษาไทย](README.th.md) · From the maker of [safe-migrations](https://github.com/nuttakulsv/safe-migrations) and [honest-tests](https://github.com/nuttakulsv/honest-tests)

AI coding agents make up package names. Attackers register those names and wait.

A 2024 study of 576,000 generated code samples found models recommending packages that don't exist **5.2% of the time for commercial models and 21.7% for open ones**, and 58% of those made-up names came back again when the same prompt was rerun. That makes them predictable, which is why the attack has a name: *slopsquatting*.

phantom-deps is a Claude Code hook (plus a skill for any agent) that checks every package against its real registry before your agent installs it.

## What it does

```
> add retry support to the API client

● Bash(pip install requests-retryer)
  ⎿ phantom-deps blocked this install:
    - requests-retryer doesn't exist on PyPI
    The name may be hallucinated. Look up the exact package name in the
    library's official docs or on the registry instead of guessing, or ask the user.
```

For each package an install command would pull in:

| What it finds | What happens |
|---|---|
| The package doesn't exist | **Blocked**, with a "did you mean ...?" if it's one typo away from a popular package |
| It exists, but was first published in the last 30 days, has under 500 downloads a week, or is a near-typo of a popular package | **You're asked first**, with the reasons |
| Well-known package (a built-in list of a few hundred) | Allowed instantly, no network call |
| The registry is slow or unreachable | Allowed. It never blocks you because PyPI had a bad minute |

It understands:

- **npm, pnpm, yarn, bun**, plus `npx`, `bunx`, `pnpm dlx`, `yarn dlx`, `npm exec`
- **pip, uv, poetry, pipx, uvx**
- **cargo** and **gem / bundle**
- **Edited manifests.** When the agent adds a line to `package.json` or `requirements.txt` and then runs a bare `npm install` or `pip install -r`, phantom-deps compares the file with the last commit and checks only what's new.

## Install

Claude Code (hook and skill):

```
/plugin marketplace add nuttakulsv/phantom-deps
/plugin install phantom-deps@phantom-deps
```

Codex, Cursor, or any agent that reads SKILL.md (skill only):

```
npx skills add nuttakulsv/phantom-deps
```

The hook needs `python3` and nothing else.

## Settings

| Environment variable | Default | Meaning |
|---|---|---|
| `PHANTOM_DEPS_MIN_AGE_DAYS` | 30 | Packages younger than this get a question |
| `PHANTOM_DEPS_MIN_WEEKLY_DOWNLOADS` | 500 | Packages with fewer weekly downloads get a question |

**Private registries:** if a command uses `--registry` or `--index-url`, or your `.npmrc`, `pip.conf`, `pyproject.toml` or Cargo config points at another index, a package that's missing from the public registry gets a question instead of a block.

## Limits

- It checks install commands and the two common manifests. It won't see a dependency installed by a script the agent writes, or one added to `pyproject.toml` and installed with `uv sync`.
- Download counts for PyPI come from pypistats.org, which sometimes lags. If it can't answer, that check is skipped.
- A real package can still be malicious. This catches the made-up and the brand-new, not a compromised maintainer. Keep using lockfiles and tools like `npm audit` or `pip-audit`.

## Tests

```
python3 hooks/phantom.py --test         # offline
python3 hooks/phantom.py --live-test    # against the real registries
python3 hooks/phantom.py check pypi requests
```

## References

- Spracklen et al., [*We Have a Package for You! A Comprehensive Analysis of Package Hallucinations by Code Generating LLMs*](https://arxiv.org/abs/2406.10279) (2024)

## License

MIT
