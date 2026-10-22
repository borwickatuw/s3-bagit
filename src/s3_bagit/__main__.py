"""Allow `python -m s3_bagit ...` invocation."""

from s3_bagit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
