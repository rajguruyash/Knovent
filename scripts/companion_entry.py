"""PyInstaller's entry point for the companion program. Kept apart from the package so the package's relative imports resolve normally."""
from faqengine.companion.app_main import main

raise SystemExit(main())
