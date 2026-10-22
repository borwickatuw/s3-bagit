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


def open_issue(brief: str | None) -> int:
    """Print a prefilled new-issue URL and try to open it; return a CLI exit code.

    *brief* stays ``str | None`` and takes no default: ``s3-bagit issue``
    declares ``brief`` with ``nargs="?"``, so a bare invocation legitimately
    passes ``None``. The caller always passes *something*, which is why the
    default is dead weight — but the ``None`` itself is load-bearing.
    """
    params = urllib.parse.urlencode(
        {"title": brief or "", "body": _build_body(brief)},
        quote_via=urllib.parse.quote,
    )
    url = f"{REPO_URL}/issues/new?{params}"
    print("Opening a new-issue page on GitHub:")
    print(f"  {url}")
    opened = webbrowser.open(url)
    if not opened:
        print(
            "(No browser available — copy the URL above into a browser, "
            "or send it to whoever maintains this tool.)"
        )
    return 0
