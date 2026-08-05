"""Allow ``python -m proxytools`` to behave like the root launcher."""

from proxytools.cli import main

raise SystemExit(main())
