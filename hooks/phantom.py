#!/usr/bin/env python3
"""phantom-deps: check packages on their public registry before a coding agent installs them.

Claude Code PreToolUse hook (Bash). For each package an install command would pull in:
  doesn't exist on the registry                     -> deny (likely a hallucinated name)
  brand new, barely downloaded, or a near-typo of a
  popular package                                   -> ask you first
  registry unreachable                              -> no decision (never blocks you because PyPI is slow)

Covers npm/pnpm/yarn/bun (+ npx, dlx), pip/uv/poetry/pipx, cargo, gem/bundle, including
`npm install` / `pip install -r` after the agent edited package.json or requirements.txt.

    python3 phantom.py --test          offline self-check
    python3 phantom.py --live-test     hits the real registries
    python3 phantom.py check pypi requests
"""
import concurrent.futures
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

MIN_AGE_DAYS = int(os.environ.get("PHANTOM_DEPS_MIN_AGE_DAYS", "30"))
MIN_WEEKLY_DOWNLOADS = int(os.environ.get("PHANTOM_DEPS_MIN_WEEKLY_DOWNLOADS", "500"))
TIMEOUT = 6
HEADERS = {"User-Agent": "phantom-deps (+https://github.com/nuttakulsv/phantom-deps)", "Accept": "application/json"}
REGISTRY = {"npm": "npm", "pypi": "PyPI", "crates": "crates.io", "gems": "RubyGems"}

POPULAR = {
    "npm": """react react-dom next vue svelte express lodash axios moment dayjs date-fns typescript webpack vite esbuild
        rollup eslint prettier jest vitest mocha chai sinon cypress playwright @playwright/test puppeteer chalk commander
        yargs inquirer dotenv cors body-parser cookie-parser jsonwebtoken bcrypt bcryptjs uuid nanoid zod yup joi ajv
        mongoose sequelize prisma @prisma/client typeorm knex pg mysql2 redis ioredis socket.io ws graphql @apollo/client
        tailwindcss postcss autoprefixer sass styled-components @emotion/react classnames clsx react-router
        react-router-dom redux @reduxjs/toolkit react-redux zustand mobx rxjs immer swr @tanstack/react-query nodemon
        ts-node tsx concurrently cross-env rimraf glob fs-extra minimist debug winston pino morgan helmet multer sharp
        cheerio node-fetch got superagent form-data qs semver bluebird async underscore ramda three d3 chart.js echarts
        leaflet firebase aws-sdk @aws-sdk/client-s3 stripe openai @anthropic-ai/sdk langchain ai hono fastify koa
        @nestjs/core electron react-native expo jquery bootstrap luxon marked markdown-it highlight.js handlebars ejs
        xlsx papaparse js-yaml yaml chokidar execa ora zx left-pad preact solid-js astro nuxt drizzle-orm""",
    "pypi": """requests urllib3 numpy pandas scipy matplotlib seaborn scikit-learn tensorflow torch torchvision keras
        transformers datasets tokenizers accelerate sentencepiece openai anthropic langchain llama-index tiktoken
        pydantic pydantic-settings fastapi uvicorn starlette flask django djangorestframework celery redis sqlalchemy
        alembic psycopg2 psycopg2-binary psycopg pymysql mysqlclient pymongo boto3 botocore google-cloud-storage httpx
        aiohttp websockets grpcio protobuf pyyaml toml tomli python-dotenv click typer rich tqdm colorama jinja2
        markupsafe werkzeug gunicorn pytest pytest-cov pytest-asyncio mock coverage tox nox black ruff flake8 pylint
        mypy isort pre-commit setuptools wheel pip virtualenv poetry packaging six attrs cryptography pyjwt bcrypt
        passlib paramiko pillow opencv-python beautifulsoup4 lxml scrapy selenium playwright python-dateutil pytz arrow
        orjson ujson jsonschema marshmallow networkx sympy statsmodels xgboost lightgbm plotly dash streamlit gradio
        jupyter notebook ipython nltk spacy pythainlp sentence-transformers chromadb faiss-cpu docker kubernetes
        loguru structlog sentry-sdk prometheus-client psutil watchdog pyinstaller cython numba polars pyarrow duckdb
        openpyxl pypdf python-docx markdown""",
    "crates": "serde serde_json tokio anyhow thiserror clap rand regex reqwest log env_logger tracing chrono itertools "
              "once_cell lazy_static futures axum hyper rayon",
    "gems": "rails rake bundler rspec puma sidekiq devise nokogiri pg redis rubocop pry faraday httparty jwt dotenv",
}


def norm(eco, name):
    return re.sub(r"[-_.]+", "-", name).lower() if eco == "pypi" else name.lower()


POPULAR = {eco: {norm(eco, n) for n in names.split()} for eco, names in POPULAR.items()}

# ---------------------------------------------------------------- parsing commands


def segments(cmd):
    """Split a shell command into simple commands at ; && || | and newlines, respecting quotes."""
    lex = shlex.shlex(cmd.replace("\\\n", " "), posix=True, punctuation_chars=";&|\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        words = list(lex)
    except ValueError:  # unbalanced quotes: best effort
        words = cmd.split()
    tokens = []
    for word in words + [";"]:
        if word and set(word) <= set(";&|\n"):
            while tokens and (re.match(r"^[A-Za-z_]\w*=", tokens[0]) or tokens[0] in ("sudo", "time", "exec", "command", "env", "nohup")):
                tokens = tokens[1:]
            if tokens:
                yield tokens
            tokens = []
        else:
            tokens.append(word)


def positional(tokens, value_flags):
    out, skip = [], False
    for t in tokens:
        if skip:
            skip = False
        elif t in value_flags:
            skip = True
        elif not t.startswith("-"):
            out.append(t)
    return out


def flag_values(tokens, flags):
    return [tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t in flags]


def npm_name(spec):
    if ":" in spec or spec.startswith((".", "/", "~")):
        return None  # npm:, git:, file:, URLs, paths
    if spec.startswith("@"):
        scope, _, rest = spec[1:].partition("/")
        if not scope or not rest:
            return None
        name = f"@{scope}/{rest.split('@')[0]}"
    elif "/" in spec:
        return None  # user/repo GitHub shorthand
    else:
        name = spec.split("@")[0]
    name = name.lower()
    return name if re.fullmatch(r"(@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*", name) else None


def pip_name(spec):
    if spec.startswith((".", "/", "~", "-")) or "://" in spec or "/" in spec or spec.endswith((".whl", ".tar.gz", ".zip")):
        return None
    name = re.split(r"[\[<>=!~;@ (]", spec, maxsplit=1)[0].strip()
    return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) else None


def plain_name(spec):
    name = spec.split("@")[0]
    return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) else None


def find_installs(tokens):
    """-> list of (ecosystem, [names], manifest_path_or_None)"""
    tool, rest = os.path.basename(tokens[0]), tokens[1:]
    sub = rest[0] if rest else ""
    found = []

    if tool in ("npm", "pnpm", "yarn", "bun"):
        npm_flags = {"--filter", "-F", "--registry", "--prefix", "--workspace", "--tag", "--cwd", "-C"}
        if tool == "npm":
            npm_flags.add("-w")
        i = 0  # skip global flags before the subcommand: npm --prefix web install
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in npm_flags else 1
        rest = rest[i:]
        sub = rest[0] if rest else ""
        if sub in ("install", "i", "add", "isntall"):
            args = positional(rest[1:], npm_flags)
            names = [n for n in map(npm_name, args) if n]
            if names:
                found.append(("npm", names, None))
            elif not args and sub != "add":
                found.append(("npm", [], "package.json"))
        elif (tool in ("pnpm", "yarn") and sub == "dlx") or (tool == "npm" and sub in ("exec", "x")):
            found += _npx(rest[1:])
    elif tool in ("npx", "bunx"):
        found += _npx(rest)

    elif tool in ("pip", "pip3") or (tool.startswith("python") and rest[:2] == ["-m", "pip"]) or (tool == "uv" and sub == "pip"):
        if "install" in tokens:
            args = tokens[tokens.index("install") + 1:]
            value_flags = {"-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url",
                           "-f", "--find-links", "-e", "--editable", "-t", "--target", "--python", "--prefix",
                           "--root", "--platform", "--python-version"}
            names = [n for n in map(pip_name, positional(args, value_flags)) if n]
            if names:
                found.append(("pypi", names, None))
            found += [("pypi", [], req) for req in flag_values(args, {"-r", "--requirement"})]
    elif (tool in ("uv", "poetry") and sub == "add") or (tool == "pipx" and sub in ("install", "run")):
        names = [n for n in map(pip_name, positional(rest[1:], {"--group", "-G", "--source", "--python", "--index"})) if n]
        found.append(("pypi", names[:1] if tool == "pipx" else names, None))
    elif tool == "uvx" or (tool == "uv" and rest[:2] in (["tool", "install"], ["tool", "run"])):
        args = positional(rest[2:] if tool == "uv" else rest, {"--from", "--with", "--python"})
        names = [pip_name(a) for a in args[:1] + flag_values(rest, {"--from", "--with"})]
        found.append(("pypi", [n for n in names if n], None))

    elif tool == "cargo" and sub in ("add", "install") and not {"--path", "--git"} & set(rest):
        found.append(("crates", [n for n in map(plain_name, positional(rest[1:], {"--features", "-F", "--version", "--vers", "--rename", "--root", "--registry"})) if n], None))
    elif (tool == "gem" and sub == "install") or (tool == "bundle" and sub == "add"):
        found.append(("gems", [n for n in map(plain_name, positional(rest[1:], {"-v", "--version", "--source", "--group"})) if n], None))
    return [f for f in found if f[1] or f[2]]


def _npx(args):
    names = flag_values(args, {"-p", "--package"}) or positional(args, set())[:1]
    names = [n for n in map(npm_name, names) if n]
    return [("npm", names, None)] if names else []


# ---------------------------------------------------------------- manifests the agent may have edited


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else None


def package_json_names(text):
    try:
        data = json.loads(text)
    except ValueError:
        return set()
    out = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        section = data.get(key) if isinstance(data, dict) else None
        for name, ver in (section.items() if isinstance(section, dict) else ()):
            if isinstance(ver, str) and not re.match(r"(file|link|workspace|git|github|npm|https?):|[^@]*/", ver):
                out.add(name.lower())
    return out


def requirement_names(text):
    return {norm("pypi", n) for n in (pip_name(line.split("#")[0].strip()) for line in text.splitlines()) if n}


def added_in_manifest(cwd, path):
    parse = package_json_names if path.endswith("package.json") else requirement_names
    try:
        with open(os.path.join(cwd, path), encoding="utf-8") as fh:
            now = parse(fh.read())
    except OSError:
        return []
    old_text = git(cwd, "show", f"HEAD:./{path}")
    new = sorted(now - (parse(old_text) if old_text is not None else set()))
    return new if old_text is not None or len(new) <= 25 else []  # untracked big manifest: not the agent's doing


def private_registry(eco, tokens, cwd):
    """Signs that packages may come from somewhere other than the public registry."""
    joined = " ".join(tokens)

    def read(path):
        try:
            with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    if eco == "npm":
        return "--registry" in joined or any("registry" in read(p) for p in (os.path.join(cwd, ".npmrc"), "~/.npmrc"))
    if eco == "pypi":
        return bool(re.search(r"(^|\s)(-i|--index-url|--extra-index-url|--index)(\s|=)", joined)
                    or any(os.environ.get(v) for v in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_INDEX"))
                    or re.search(r"tool\.poetry\.source|tool\.uv\.index|index-url", read(os.path.join(cwd, "pyproject.toml")))
                    or any("index" in read(p) for p in ("~/.config/pip/pip.conf", "~/.pip/pip.conf", "/etc/pip.conf")))
    if eco == "crates":
        return "--registry" in joined or "[registries" in read("~/.cargo/config.toml")
    return "--source" in joined


# ---------------------------------------------------------------- registries


class Missing(Exception):
    pass


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise Missing() from None
        raise


def when(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def lookup(eco, name):
    """-> {"created": datetime|None, "weekly": int|None, "total": int|None}. Raises Missing."""
    if eco == "npm":
        q = urllib.parse.quote(name, safe="@")
        doc = get(f"https://registry.npmjs.org/{q}")
        if not doc.get("versions"):
            raise Missing()  # unpublished
        try:
            weekly = get(f"https://api.npmjs.org/downloads/point/last-week/{name}").get("downloads")
        except Exception:
            weekly = None
        return {"created": when(doc.get("time", {}).get("created")), "weekly": weekly, "total": None}
    if eco == "pypi":
        doc = get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
        times = [f["upload_time_iso_8601"] for files in doc.get("releases", {}).values() for f in files]
        if not times:
            raise Missing()  # name registered, nothing installable
        try:
            weekly = get(f"https://pypistats.org/api/packages/{norm('pypi', name)}/recent")["data"]["last_week"]
        except Exception:
            weekly = None
        return {"created": when(min(times)), "weekly": weekly, "total": None}
    if eco == "crates":
        c = get(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")["crate"]
        return {"created": when(c.get("created_at")), "weekly": (c.get("recent_downloads") or 0) // 13, "total": None}
    doc = get(f"https://rubygems.org/api/v1/gems/{urllib.parse.quote(name)}.json")
    versions = get(f"https://rubygems.org/api/v1/versions/{urllib.parse.quote(name)}.json")
    created = min((v["created_at"] for v in versions), default=None)
    return {"created": when(created), "weekly": None, "total": doc.get("downloads")}


def distance(a, b, cap=2):
    """Optimal string alignment distance (typos incl. swapped letters), stops counting past cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def lookalike(eco, name):
    n = norm(eco, name)
    if len(n) < 4:
        return None
    limit = 2 if len(n) >= 10 else 1
    return next((p for p in sorted(POPULAR[eco]) if p != n and distance(n, p) <= limit), None)


def assess(eco, name, private=False, now=None, lookup=lookup):
    """-> (verdict, reason); verdict is deny, ask, ok or unknown."""
    if norm(eco, name) in POPULAR[eco]:
        return "ok", ""
    now = now or dt.datetime.now(dt.timezone.utc)
    hint = lookalike(eco, name)
    try:
        info = lookup(eco, name)
    except Missing:
        msg = f"{name} doesn't exist on {REGISTRY[eco]}" + (f" (did you mean {hint}?)" if hint else "")
        return ("ask", msg + "; it may be on a private registry") if private else ("deny", msg)
    except Exception:
        return "unknown", ""
    reasons = []
    if info["created"] and (now - info["created"]).days < MIN_AGE_DAYS:
        reasons.append(f"first published {(now - info['created']).days} days ago")
    if info["weekly"] is not None and info["weekly"] < MIN_WEEKLY_DOWNLOADS:
        reasons.append(f"{info['weekly']} downloads last week")
    if info["total"] is not None and info["total"] < MIN_WEEKLY_DOWNLOADS * 20:
        reasons.append(f"{info['total']} downloads in total")
    if hint and reasons:
        reasons.append(f"one typo away from {hint}")
    return ("ask", f"{name} on {REGISTRY[eco]}: " + ", ".join(reasons)) if reasons else ("ok", "")


# ---------------------------------------------------------------- hook


def plan(cmd, cwd, depth=0):
    """-> [(ecosystem, name, private)], following `cd` and `sh -c` / `eval` wrappers."""
    jobs = []
    for tokens in segments(cmd):
        tool = os.path.basename(tokens[0])
        if tool == "cd":
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(tokens[1] if len(tokens) > 1 else "~")))
            continue
        if tool in ("bash", "sh", "zsh", "eval") and depth < 3:
            inner = tokens[tokens.index("-c") + 1] if "-c" in tokens[:-1] else " ".join(tokens[1:]) if tool == "eval" else None
            jobs += plan(inner, cwd, depth + 1) if inner else []
            continue
        for eco, names, manifest in find_installs(tokens):
            here = os.path.join(cwd, (flag_values(tokens, {"--prefix", "-C", "--cwd", "--dir"}) or ["."])[0])
            private = private_registry(eco, tokens, here)
            jobs += [(eco, name, private) for name in names or added_in_manifest(here, manifest)]
    return jobs


def check_command(cmd, cwd, lookup=lookup):
    jobs = [j for j in dict.fromkeys(plan(cmd, cwd)) if norm(j[0], j[1]) not in POPULAR[j[0]]]
    if not jobs:
        return None
    if len(jobs) > 20:  # don't silently check only some of them
        return "ask", (f"phantom-deps: this installs {len(jobs)} packages outside its well-known list, "
                       "too many to check one by one. Review them before allowing.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: assess(j[0], j[1], j[2], lookup=lookup), jobs))
    denied = [r for v, r in results if v == "deny"]
    asked = [r for v, r in results if v == "ask"]
    if denied:
        return "deny", ("phantom-deps blocked this install:\n- " + "\n- ".join(denied + asked) +
                        "\nThe name may be hallucinated. Look up the exact package name in the library's official docs "
                        "or on the registry instead of guessing, or ask the user.")
    if asked:
        return "ask", "phantom-deps: check before installing:\n- " + "\n- ".join(asked)
    return None


def handle(data, lookup=lookup):
    if data.get("tool_name") not in (None, "Bash"):
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    verdict = check_command(cmd, data.get("cwd") or os.getcwd(), lookup)
    if verdict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": verdict[0], "permissionDecisionReason": verdict[1]}}
    return None


# ---------------------------------------------------------------- self-checks


def test():
    import tempfile
    parse = lambda c: [f for t in segments(c) for f in find_installs(t)]  # noqa: E731
    cases = {
        "npm install left-pad": [("npm", ["left-pad"], None)],
        "npm i -D @types/node@20 typescript": [("npm", ["@types/node", "typescript"], None)],
        "pnpm add --filter web react-query-pro": [("npm", ["react-query-pro"], None)],
        "yarn add lodash@^4": [("npm", ["lodash"], None)],
        "cd app && npm install": [("npm", [], "package.json")],
        "npx create-vite-pro my-app": [("npm", ["create-vite-pro"], None)],
        "npx -y -p cowsay-pro cowsay hi": [("npm", ["cowsay-pro"], None)],
        "pip install 'requests==2.31' 'fastapi[all]'": [("pypi", ["requests", "fastapi"], None)],
        "python3 -m pip install -r requirements.txt": [("pypi", [], "requirements.txt")],
        "uv add pydantic-settingz": [("pypi", ["pydantic-settingz"], None)],
        "uvx ruff check .": [("pypi", ["ruff"], None)],
        "poetry add httpx --group dev": [("pypi", ["httpx"], None)],
        "cargo add serde tokio --features full": [("crates", ["serde", "tokio"], None)],
        "gem install rails -v 7.1": [("gems", ["rails"], None)],
        "FOO=1 sudo npm i axios": [("npm", ["axios"], None)],
        "pip install -e .": [],
        "npm install ./local-pkg user/repo git+https://x/y.git": [],
        "cargo install --path .": [],
        "git commit -m 'npm install foo'": [],
        "npm run build": [],
    }
    for cmd, want in cases.items():
        assert parse(cmd) == want, (cmd, parse(cmd))

    now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    db = {("npm", "fresh-pkg"): {"created": now - dt.timedelta(days=3), "weekly": 12, "total": None},
          ("npm", "old-solid"): {"created": now - dt.timedelta(days=900), "weekly": 90000, "total": None},
          ("pypi", "reqeusts"): None, ("pypi", "requestz"): {"created": now - dt.timedelta(days=5), "weekly": 3, "total": None}}

    def fake(eco, name):
        if (eco, name) in db and db[(eco, name)] is None or (eco, name) not in db and name.startswith("ghost"):
            raise Missing()
        if name == "flaky":
            raise TimeoutError()
        if (eco, name) not in db:
            raise AssertionError(f"unexpected lookup {eco}:{name}")
        return db[(eco, name)]

    assert assess("pypi", "reqeusts", now=now, lookup=fake) == ("deny", "reqeusts doesn't exist on PyPI (did you mean requests?)")
    v, r = assess("pypi", "requestz", now=now, lookup=fake)
    assert v == "ask" and "5 days" in r and "3 downloads" in r and "requests" in r, r
    assert assess("npm", "react", lookup=fake) == ("ok", "")  # popular: no network at all
    assert assess("npm", "old-solid", now=now, lookup=fake) == ("ok", "")
    assert assess("npm", "flaky", now=now, lookup=fake)[0] == "unknown"
    assert assess("npm", "ghost-lib", private=True, now=now, lookup=fake)[0] == "ask"
    assert distance("reqeusts", "requests") == 1 and distance("lodash", "lodahs") == 1 and distance("abc", "xyz") == 3

    out = handle({"tool_name": "Bash", "tool_input": {"command": "pip install requests reqeusts"}, "cwd": "/"}, lookup=fake)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "reqeusts" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert handle({"tool_name": "Bash", "tool_input": {"command": "npm i flaky"}, "cwd": "/"}, lookup=fake) is None
    assert handle({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"}, lookup=fake) is None

    with tempfile.TemporaryDirectory() as d:
        g = lambda *a: subprocess.run(["git", *a], cwd=d, capture_output=True, check=True)  # noqa: E731
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        with open(os.path.join(d, "package.json"), "w") as fh:
            json.dump({"dependencies": {"react": "^18"}}, fh)
        with open(os.path.join(d, "requirements.txt"), "w") as fh:
            fh.write("requests==2.31\n")
        g("add", ".")
        g("commit", "-qm", "base")
        with open(os.path.join(d, "package.json"), "w") as fh:
            json.dump({"dependencies": {"react": "^18", "ghost-ui": "^1", "mine": "file:../mine"}}, fh)
        with open(os.path.join(d, "requirements.txt"), "w") as fh:
            fh.write("requests==2.31\nreqeusts>=1  # typo\n-r other.txt\n")
        assert added_in_manifest(d, "package.json") == ["ghost-ui"]
        assert added_in_manifest(d, "requirements.txt") == ["reqeusts"]
        decide = lambda c, cwd=d: (handle({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": cwd}, lookup=fake)  # noqa: E731
                                   or {}).get("hookSpecificOutput", {}).get("permissionDecision")
        assert decide("npm install") == "deny"
        # follows cd, --prefix and shell wrappers to the right manifest
        parent = os.path.dirname(d)
        assert decide(f"cd {os.path.basename(d)} && npm install", parent) == "deny"
        assert decide(f"npm --prefix {os.path.basename(d)} install", parent) == "deny"
        assert decide("bash -c 'pip install reqeusts'", "/") == "deny"
        assert decide("env CI=1 sh -c \"cd /tmp; npm i ghost-x\"", "/") == "deny"
        # too many unknown packages: ask instead of silently checking a subset
        assert decide("npm i " + " ".join(f"pkg-{i}" for i in range(25)), "/") == "ask"
        # malformed manifests don't crash the hook
        assert package_json_names('["not", "an", "object"]') == set()
        assert package_json_names('{"dependencies": ["x"]}') == set()
    print("ok")


def live_test():
    for eco, name, want in [("npm", "is-number", "ok"), ("npm", "phantom-deps-surely-not-a-real-pkg-7x", "deny"),
                            ("pypi", "requests-oauthlib", "ok"), ("pypi", "phantom-deps-surely-not-real-7x", "deny"),
                            ("crates", "rand_core", "ok"), ("crates", "phantom-deps-not-real-7x", "deny"),
                            ("gems", "rack", "ok"), ("gems", "phantom-deps-not-real-7x", "deny")]:
        verdict, reason = assess(eco, name)
        print(f"{eco:7} {name:42} {verdict:8} {reason}")
        assert verdict == want, (eco, name, verdict, reason)
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        test()
    elif sys.argv[1:] == ["--live-test"]:
        live_test()
    elif sys.argv[1:2] == ["check"] and len(sys.argv) == 4:
        print(assess(sys.argv[2], sys.argv[3]))
    else:
        raw = sys.stdin.read()
        try:
            result = handle(json.loads(raw))
        except Exception as e:  # a bug here must not wave installs through silently, or block everything else
            looks_like_install = re.search(r"\b(install|add|dlx|exec|npx|bunx|uvx)\b", raw)
            result = looks_like_install and {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": f"phantom-deps couldn't check this command ({e}). Review it before allowing."}}
        if result:
            print(json.dumps(result))
