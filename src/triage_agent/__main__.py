"""`python -m triage_agent` の入口。コンソールスクリプト（`triage-agent`）と同じ `main` を使う。"""

from triage_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
