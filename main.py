name: NBA Bot

on:
  workflow_dispatch:

jobs:
  test_discord:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: |
          python -V
          pip install -r requirements.txt || true

      - name: Print secret presence (masked)
        env:
          DISCORD_TEAM_WEBHOOK: ${{ secrets.DISCORD_TEAM_WEBHOOK }}
          DISCORD_PROPS_WEBHOOK: ${{ secrets.DISCORD_PROPS_WEBHOOK }}
          DISCORD_LOG_WEBHOOK: ${{ secrets.DISCORD_LOG_WEBHOOK }}
        run: |
          echo "TEAM=${DISCORD_TEAM_WEBHOOK:0:4}...${DISCORD_TEAM_WEBHOOK: -4}"
          echo "PROPS=${DISCORD_PROPS_WEBHOOK:0:4}...${DISCORD_PROPS_WEBHOOK: -4}"
          echo "LOG=${DISCORD_LOG_WEBHOOK:0:4}...${DISCORD_LOG_WEBHOOK: -4}"

      - name: Run main.py (discord test)
        env:
          DISCORD_TEAM_WEBHOOK: ${{ secrets.DISCORD_TEAM_WEBHOOK }}
          DISCORD_PROPS_WEBHOOK: ${{ secrets.DISCORD_PROPS_WEBHOOK }}
          DISCORD_LOG_WEBHOOK: ${{ secrets.DISCORD_LOG_WEBHOOK }}
        run: python main.py
