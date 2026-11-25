# Contributing

Thanks for looking. This page says how to run things, what a change needs before it is merged,
and the terms a contribution comes in under.

## Run it

Every part runs on made-up data with nothing to connect to; the commands are in the
[README](README.md#try-it). You need Python 3.12 or newer, and Docker only for the Kubernetes
demo script and for building images.

Each part keeps its own virtual environment and its own tests:

| Part | Tests |
|---|---|
| Shared core | `python -m pytest -q core/py/tests` |
| Kubernetes module | `python -m pytest -q modules/kubernetes/tests` |
| Network module | `python -m pytest -q modules/network/tests` |
| ESXi module | `python -m pytest -q modules/esxi/tests` |
| Suite bar and Settings | `python -m pytest -q shell/tests` |

Install a part's `requirements.txt` plus `pytest` and `httpx` first. The same five suites run on
every push and pull request.

## Where things go

- **Something every module needs** belongs in [`core/py/galaxy_core`](core). Settings, the graph
  format, the tables and the settings API live there.
- **Something about one kind of system** belongs in that module: where its data comes from
  (`collector/`), how it is drawn (`web/`), how it is deployed (`deploy/`).
- **A new module** starts as a copy of the shape of [`modules/esxi`](modules/esxi), the first one
  built on the core. Open a "new module" issue first so the map and the checks can be talked
  through before code is written.

## What a change needs

- **Tests** for what it changes, in that part's `tests/`. The sample and demo data are the
  fixtures: a new check should have something in the sample that triggers it.
- **Read-only by default.** A module watches; it does not change what it watches. Anything that
  does (the Network module's block list is the one example) is opt-in, behind a password, and
  designed so the watched system pulls the change and can refuse it.
- **Plain words on the page.** A finding says what was seen, why it matters and what to do, for
  someone who does not know the product's internals. Include the evidence it rests on.
- **No real environments in the repository.** Addresses, host names, account names, captured
  logs and screenshots come from the sample or demo data, or from documentation ranges
  (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `example.net`).
- **No secrets, ever.** Not in code, tests, manifests or commit messages. See
  [Secrets](README.md#secrets).
- **The changelog and version** of the part you changed (`CHANGELOG.md`, `VERSION`), when
  behaviour changes.

## Licence of contributions

Workload Galaxy is licensed under the [GNU Affero General Public License v3.0](LICENSE).

By submitting a contribution you agree that:

1. it is your own work, or you have the right to submit it;
2. you keep your copyright in it;
3. you license it to the project under AGPL-3.0, and you also give the project's maintainer the
   right to license it under other terms, including commercial ones.

The third point is what lets the project be offered under a commercial licence to people who
cannot use AGPL software, which is how its development is meant to be paid for. If you are not
comfortable with it, open an issue describing the change instead and it can be written
independently.

## Security problems

Please do not open a public issue. See [SECURITY.md](SECURITY.md).
