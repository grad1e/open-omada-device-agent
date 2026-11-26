# Contributing to Open Omada Device Agent

Thank you for helping improve open Omada interoperability.

This repository is a fork of
[adryanalencar/open-omada-device-agent](https://github.com/adryanalencar/open-omada-device-agent),
developed independently under the same Apache-2.0 license. `develop` is the
main branch; `main` mirrors upstream.

## Principles

Contributions should be independently implemented, reproducible, and grounded
in observable protocol behavior. Please do not submit proprietary vendor source
code, decompiled source files, firmware images, private keys, certificates, or
confidential controller data.

When documenting a protocol field or state transition, label the evidence as
one of:

- **Observed** — directly seen on the wire or in controller/device output.
- **Validated** — reproduced in a controlled interoperability test.
- **Inferred** — strongly suggested by behavior but not yet independently proven.
- **Unknown** — field or behavior exists, but semantics are not established.

## AI-assisted changes

Changes written with AI coding tools are welcome. The person submitting them
is responsible for them, as for any other change:

- you have read and understood every line;
- the tests cover the change and pass;
- behavior changes were tried on a real controller and access point, and the
  PR says what was tested (see below);
- protocol claims carry an evidence label, not the tool's guess.

Agents should follow [AGENTS.md](AGENTS.md).

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
```

## Testing on hardware

Unit tests do not talk to a controller. For a behavior change, install the
agent on a lab access point with `scripts/deploy-openwrt.sh` (see
[docs/install-openwrt.md](docs/install-openwrt.md)) and check:

1. the agent log (`logread -e open_omada`) shows `INFORM_RESPONSE` and no
   tracebacks;
2. the controller log has no new `ERROR` or `Exception` lines;
3. the change took effect on the AP (`uci show`, `iw dev`, `ip addr`).

Use a lab controller, never a production one.

## Branches and merging

`develop` is protected: every change, however small, lands through a pull
request whose CI checks pass. Nobody pushes to it directly.

1. Branch from the latest `develop`:
   `git switch -c fix/short-name develop` (prefixes `feat/`, `fix/`, `docs/`,
   `refactor/`, `chore/`).
2. Commit there (see [Commit style](#commit-style)). Rewriting history with
   `git commit --fixup` and `git rebase -i --autosquash` is fine on your own
   branch until it is merged.
3. Push the branch and open a pull request into `develop`.
4. When CI passes, merge with **Rebase and merge**, the only method enabled.
   It keeps every commit and a linear history. The branch is deleted
   automatically.

If `develop` moved on while your PR was open, rebase the branch on it
(`git rebase develop`) and force-push the branch; CI must pass on the
rebased commits.

`main` mirrors upstream. To bring in upstream changes, update `main` from
upstream, then merge it into `develop` through a `chore/sync-upstream`
branch and pull request.

## Pull requests

Keep protocol changes small enough to review. A good PR includes:

1. a short description of the observed behavior;
2. a test or reproducible lab procedure;
3. the implementation change;
4. documentation updates when the wire contract changes
   (`docs/protocol-status.md`, `docs/configuration.md`);
5. a `CHANGELOG.md` entry under **Unreleased** for user-visible changes;
6. what it was tested on: controller version, AP hardware, OpenWrt version.

Do not include real controller addresses, MAC addresses, SSIDs, account
names, passwords, session cookies, tokens, or production packet captures. Use
`02:aa:bb:cc:dd:01`-style MACs and `192.0.2.x` addresses in examples.

## Commit style

Use [Conventional Commits](https://www.conventionalcommits.org/), one logical
change per commit, with its tests in the same commit. Examples:

```text
feat(protocol): handle incremental SET config versions
fix(reconnect): recover stale controller context with rediscovery
docs(ecsp): document PRE_CONNECT negotiation
```
