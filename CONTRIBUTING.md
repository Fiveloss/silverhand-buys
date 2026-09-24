# Contributing

Thanks for helping! Bug reports, fixes and support for more launchpads are all welcome.

## Before you open a pull request

1. `python3 tests.py`: standard library only, no network. It must pass.
2. `pip install -r requirements.txt && python3 tests_bot.py` for anything that
   touches `bot.py`, `sender.py` or `main.py`.
3. If you change a chain fact (an address, an event layout, a selector), add a
   real mainnet log or call result to `fixtures/` and a test that decodes it.
   Run `python3 tools/preflight.py` against the live chain and paste its output
   in the PR.

## House rules

- **Never make up a number.** If a value cannot be read from the chain, leave it
  out of the post. Do not show it as zero.
- **No keys, no trading.** The bot must never ask for, store or use private keys
  or seed phrases.
- **Be polite to the public RPC.** No per-trade calls. New reads go through
  `rpc.Rpc` and are budgeted per tick, not per event.
- **Telegram rules:** digital goods are sold for Telegram Stars only. Settings
  commands stay admin-only.
- Match the style of the code around you: small pure functions where possible,
  comments that say *why*.

## License

By contributing you agree that your contribution is licensed under the
[AGPL-3.0](LICENSE), like the rest of the project.
