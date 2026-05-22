"""Allow `python -m kopah_bagit ...` invocation."""

from kopah_bagit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
