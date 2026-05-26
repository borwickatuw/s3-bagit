"""``s3-bagit issue`` — open a pre-filled GitHub new-issue URL.

Goal: turn "give up and email John" into a one-command path to a
properly-tagged report. The default browser pops open with the title and
body pre-filled with environment info; if no browser is available (SSH
session, container), the URL is printed to stdout so the operator can
copy-paste.
"""

import platform
import textwrap
import urllib.parse
import webbrowser

from s3_bagit import REPO_URL, __version__


def _build_body(brief: str | None) -> str:
    body = f"""\
**What happened**
{brief or "(describe what you were doing and what went wrong)"}

**Steps to reproduce**
1.
2.
3.

**Expected vs. actual**

**Environment**
- s3-bagit: {__version__}
- Python:   {platform.python_version()}
- OS:       {platform.platform()}
"""
    return textwrap.dedent(body)


def build_issue_url(brief: str | None = None) -> str:
    """Return a GitHub new-issue URL with prefilled title and body."""
    title = brief or ""
    params = urllib.parse.urlencode(
        {"title": title, "body": _build_body(brief)},
        quote_via=urllib.parse.quote,
    )
    return f"{REPO_URL}/issues/new?{params}"


def open_issue(brief: str | None = None) -> int:
    """Print the URL and try to open it in a browser; return a CLI exit code."""
    url = build_issue_url(brief)
    print("Opening a new-issue page on GitHub:")
    print(f"  {url}")
    opened = webbrowser.open(url)
    if not opened:
        print(
            "(No browser available — copy the URL above into a browser, "
            "or send it to whoever maintains this tool.)"
        )
    return 0
