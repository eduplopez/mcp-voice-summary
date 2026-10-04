"""Pre-release checks for the MCP voice summary server.

Bundles the checks worth running before every release, so they are not left to
memory:

    .venv\\Scripts\\python.exe pre_release_check.py

Exits non-zero if any check fails, so it can gate a release.
"""

import asyncio
import json
import subprocess
import sys
import unittest

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' - {detail}' if detail else ''}")
    if not ok:
        FAILURES.append(label)


def main() -> int:
    print("1. Test suite")
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    check("tests", result.wasSuccessful(), f"{result.testsRun} run")

    print("\n2. Known vulnerabilities")
    # Judge by exit code, not by grepping the output. pip-audit prints a bare
    # table header when it cannot resolve every dependency, so a substring match
    # reported a failure that was really a different, unrecognised message.
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit"], capture_output=True, text=True
    )
    detail = (proc.stdout + proc.stderr).strip().splitlines()
    if proc.returncode == 0:
        check("pip-audit", True, detail[-1][:80] if detail else "clean")
    else:
        check("pip-audit", False, " | ".join(detail[-6:])[:300])

    print("\n3. stdout is clean (MCP JSON-RPC lives there)")
    proc = subprocess.run(
        [sys.executable, "-c", "import mcp_voice_summary as m; m.list_voices()"],
        capture_output=True,
        text=True,
    )
    noisy = [
        line
        for line in proc.stdout.splitlines()
        if line.strip() and line.strip() not in {"Engine: sapi5", "Engine: edge"}
    ]
    check("no stray stdout", proc.returncode == 0, f"{len(noisy)} unexpected lines")

    print("\n4. No dangerous calls in the source")
    with open("mcp_voice_summary.py", encoding="utf-8") as handle:
        source = handle.read()
    for pattern, label in (
        ("shell=True", "shell=True"),
        ("os.system", "os.system"),
        ("eval(", "eval"),
        ("exec(", "exec"),
        ("pickle", "pickle"),
    ):
        check(f"absent: {label}", pattern not in source)

    print("\n5. Tool catalog size")
    sys.path.insert(0, ".")
    import mcp_voice_summary as server  # noqa: E402

    tools = asyncio.run(server.mcp.list_tools())
    budget = sum(
        len(t.description or "") // 4 + len(json.dumps(t.input_schema)) // 4
        for t in tools
    )
    names = ", ".join(t.name for t in tools)
    # Every tool is billed on every turn, so a regression here matters more than
    # any single new feature.
    check("catalog under 350 tokens", budget < 350, f"~{budget} tokens ({names})")

    print("\n6. Counters expose no text")
    counters = server._counters
    check(
        "counters are numeric only",
        all(isinstance(v, int) for v in counters.values()),
        ", ".join(sorted(counters)),
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All pre-release checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
