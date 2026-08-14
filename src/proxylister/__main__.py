"""Allow ``python -m proxylister`` to behave like the root launcher."""

from proxylister.cli import main

raise SystemExit(main())
