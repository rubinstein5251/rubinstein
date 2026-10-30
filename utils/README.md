# utils

This folder contains the utility modules required by the supplied three-engine bot:

- `momentum_inventory.py` — weighted-average YES/NO inventory and realized PnL.
- `momentum_risk.py` — entry-window, fill, and stop-loss helpers.
- `clob_helpers.py` — price clamping and CLOB order-type conversion.

Copy the entire `utils/` directory beside `bot.py`, `config.py`, `main.py`, and `strategies/`.
