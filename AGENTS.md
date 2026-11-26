# Agent guide

Instructions for AI coding agents (and humans) working in this repository.
Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) too.

## What this is

An open-source agent that makes an OpenWrt access point look like an Omada
EAP to an Omada controller: discovery, adoption, config (SET), and telemetry
(INFORM) over TP-Link's ECSP protocol. Python 3.11+, no runtime dependencies
beyond `python-dotenv` and `psutil`.

## Commands

```bash
python -m pip install -e '.[dev]'
pytest -q                 # all tests; must pass before every commit
ruff check .              # lint (same as CI); must pass before every commit
bash -n scripts/deploy-openwrt.sh   # after editing the deploy script
```

Deploy to a lab AP: `scripts/deploy-openwrt.sh --host root@<ap> [--env FILE]`
(see the README's install section). Agent log on the AP:
`logread -e open_omada`.

## Layout

- `src/open_omada_device_agent/adapters/inbound/ecsp/` — controller messages
  to domain objects (`mappers/configuration.py` parses SET bodies).
- `src/open_omada_device_agent/adapters/outbound/openwrt/` — applies config to
  OpenWrt (`uci.py` wireless, `ip_settings.py`, `snmp.py`, `opennds.py`, ...).
- `src/open_omada_device_agent/contexts/` — domain models per area.
- `src/open_omada_device_agent/application/` — use cases and ports.
- `src/open_omada_device_agent/adoption.py`, `discovery.py` — the ECSP session.
- `tests/` — one test file per feature; no network or device needed.
- `docs/protocol-status.md` — what is implemented; update it with every
  feature.

## Rules

1. **No vendor material.** Never add TP-Link source, decompiled code,
   firmware, certificates, keys or controller data. Implement from observed
   behavior only.
2. **No real identifiers.** Use `02:aa:bb:cc:dd:01`-style MACs, `192.0.2.x`
   / `198.51.100.x` addresses and made-up SSIDs in code, tests and docs. Never
   write passwords, Wi-Fi keys or tokens anywhere, including logs you paste.
3. **Advertise only what works.** Do not add a capability, component or
   config key to what the agent reports unless the agent really applies it.
4. **Label evidence** in docs and PRs: Observed, Validated, Inferred or Unknown
   (see CONTRIBUTING.md).
5. **Risky changes are opt-in.** Anything that can cut the AP off its network
   (IP, VLAN, firewall, portal on the uplink) needs a guard, a rollback or an
   explicit `OMADA_CAP_*` switch that defaults to off.

## Commits

- Work on `develop` or a branch from it. `main` mirrors upstream.
- One logical change per commit, with its tests in the same commit. Each
  commit must pass `pytest` and `ruff`.
- [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(scope): ...`, `fix(scope): ...`, `docs(scope): ...`, imperative mood.
- Add a `CHANGELOG.md` entry under **Unreleased** for every user-visible
  change.
- Stage files by name (`git add <file>`), not `git add .`, and read
  `git diff --cached` before committing.
- Keep refactors, formatting and features in separate commits.
- Never rewrite shared history, force-push, or run `git reset --hard`,
  `git clean` or `git checkout -- .` without the owner's say-so. Leave changes
  you did not make alone.
- Do not commit `.env*` (except `.env.example`), `.omada-*` state files,
  packet captures, dumps or other temporary files.

## Verifying on hardware

Tests do not cover the controller. For behavior changes, check on a lab
controller and AP: deploy, watch for `INFORM_RESPONSE` in the agent log, look
for `ERROR`/`Exception` in the controller's `server.log`, and confirm the
effect on the AP (`uci`, `iw`, `ip`). Never test on a production controller.
