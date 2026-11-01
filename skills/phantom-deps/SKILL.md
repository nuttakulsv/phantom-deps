---
name: phantom-deps
description: Use before adding, installing, or suggesting any third-party package or dependency (npm, pnpm, yarn, bun, npx, pip, uv, poetry, pipx, cargo, gem, bundle), and whenever an install is blocked by the phantom-deps hook. Prevents installing hallucinated or look-alike package names (slopsquatting).
---

# Phantom deps

Language models suggest packages that don't exist. Attackers register those names and wait for someone's agent to install them. Your memory of a package name is not evidence that it exists.

## Before adding a dependency

1. **Do you need it?** Check whether the standard library or an existing dependency already does the job. Each new package adds risk.
2. **Get the exact name from a source, not from memory.** Use the library's official docs, its repository README, or the registry page. If you can't verify it, say so and ask the user.
3. **Check that it's established.** On the registry, look at the publish history, weekly downloads, a linked source repository, and maintainers. Be suspicious of something published in the last few weeks, or with almost no downloads, especially if its name is close to a popular package.
4. **Watch for look-alikes:** `reqeusts`, `python-dateutils`, `lodahs`, `@type/node`, and scopes that imitate real ones.
5. **Pin a version** and let the lockfile record it. Don't `npx` or `uvx` an unfamiliar package just to try it.

## When the phantom-deps hook blocks or questions an install

- **Don't retry variations of the name until one installs.** That's exactly how look-alike packages get installed.
- Find the real name in the official docs, or check whether the feature is in a package you already have.
- If the user's project uses a private registry, tell the user. The hook only knows public registries.
- Tell the user what was flagged and why, in one or two lines.

## In your final message

List any dependency you added, with its name and version, and where you confirmed the name.
