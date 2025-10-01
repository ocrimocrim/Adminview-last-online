name: beQuiet last seen

on:
  schedule:
    - cron: "0 * * * *"        # stündlich UTC
  workflow_dispatch:

jobs:
  run-tracker:
    runs-on: ubuntu-latest
    env:
      TZ: Europe/Berlin
      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
      MODE: auto
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4

      - name: Run tracker
        run: python bequiet_last_seen.py
