from __future__ import annotations

import argparse

from src.apps.claimer.process import run_claim_process
from src.shared.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Claimer")
    parser.add_argument("--dry-run", action="store_true", help="Log positions without submitting transactions")
    args = parser.parse_args()

    settings = get_settings()
    if args.dry_run:
        settings.dry_run = True

    run_claim_process(settings)


if __name__ == "__main__":
    main()
