# Contributing

Thanks for helping. Bug reports, fixes, client work and compatibility reports (which NAS, which SMB settings) are
all welcome.

* **Security problems:** not in an issue. See [SECURITY.md](SECURITY.md).
* **Bugs:** open an issue with the version, your platform, your SMB server (make and version) and the steps to see it.
  Leave out addresses, account names and pins.
* **Changes:** open a pull request against `main`. Keep it to one thing. Add or update tests:
  * gateway: `cd gateway && pip install -e '.[test]' && pytest`
  * API changes: change `gateway/loft_gateway/api.py`, then `python api/gen_openapi.py` (CI fails if
    `api/openapi.yaml` is out of date) and extend `conformance/`.
  * a client: it must pass the conformance suite's behaviour (pins, refresh, change feed, uploads, conflicts).
* **Style:** follow the code around you. Short, plain UI text. No private data in code, tests or commits:
  CI runs gitleaks on every push.
* **Building your own apps (a fork):** the Apple team id (`5A3JX4D37B`), the bundle ids and app group
  (`no.incremental.loft…`, `group.no.incremental.loft`) in `apple/project.yml`, the entitlements and
  `LoftConfig.swift`, and the Android application id, are Incremental's. A fork that signs and ships its own builds
  sets its own team id and ids, and its own name and icons ([TRADEMARKS.md](TRADEMARKS.md)).
* By sending a contribution you agree that it is licensed under the Apache License 2.0 (section 5 of
  [LICENSE](LICENSE)). The Loft name and icons stay ours ([TRADEMARKS.md](TRADEMARKS.md)).
