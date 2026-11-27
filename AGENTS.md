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
(see [docs/install-openwrt.md](docs/install-openwrt.md)). Agent log on the AP:
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
   behavior only. One exception: the AP model table
   (`contexts/device/omada_models_data.py`) holds a few facts per model
   (bands, streams, power range, widest channel, Wi-Fi generation, DFS)
   generated from a controller's model templates by
   `scripts/gen_omada_models.py`, credited in NOTICE. More facts may be added
   there when a feature needs them; device identifiers, firmware data,
   feature lists and raw template exports may not.
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

- Never commit to `develop` directly; it is protected. Branch from it
  (`feat/`, `fix/`, `docs/`, `refactor/`, `chore/` + short name), open a PR
  into `develop`, and merge with Rebase and merge once CI passes. `main`
  mirrors upstream. Details: CONTRIBUTING.md, "Branches and merging".
- One logical change per commit, with its tests in the same commit. Each
  commit must pass `pytest` and `ruff`.
- [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(scope): ...`, `fix(scope): ...`, `docs(scope): ...`, imperative mood.
- Add a `CHANGELOG.md` entry under **Unreleased** for every user-visible
  change.
- Stage files by name (`git add <file>`), not `git add .`, and read
  `git diff --cached` before committing.
- Keep refactors, formatting and features in separate commits.
- Never rewrite `develop` or `main`, force-push them, or run `git reset --hard`,
  `git clean` or `git checkout -- .` without the owner's say-so. Leave changes
  you did not make alone.
- Do not commit `.env*` (except `.env.example`), `.omada-*` state files,
  packet captures, dumps or other temporary files.

## Reviewing pull requests

Every AI agent that opens a pull request must review it right after
creating it, before doing anything else:

1. Read the diff: `gh pr diff <number> -R <owner>/<repo>`.
2. Look for correctness bugs, controller-protocol mismatches (field names,
   types, units), OpenWrt changes that could cut the AP off its network,
   shell injection in commands run on the device, secrets in code or logs,
   and missing tests. Check the Rules and Commits sections above.
3. Report only real problems, with file and line, to the person you are
   working with. Do not post PR comments or push fixes unless they ask.

Claude Code runs this automatically through `.claude/hooks/review-new-pr.sh`;
other agents follow this section.

## Verifying on hardware

Tests do not cover the controller. For behavior changes, check on a lab
controller and AP: deploy, watch for `INFORM_RESPONSE` in the agent log, look
for `ERROR`/`Exception` in the controller's `server.log`, and confirm the
effect on the AP (`uci`, `iw`, `ip`). Never test on a production controller.

## Long tasks and handoffs

Agent sessions get compacted or restarted in the middle of a task. Work so
that nothing is lost when that happens:

1. **Keep a handoff note** outside the repository (your agent's memory or
   a local notes file, never a committed file). Keep it short: the task, the
   branch and PR, what you found so far, and the next step. Update it in
   place; don't append a growing log.
2. **Write findings down as you find them.** During research or a
   multi-step change, add each finding and the next step to the note right
   away, not at the end.
3. **After a reset, re-check before continuing:** read the note, then
   `git status`, the current branch, `git log --oneline develop..HEAD`, the
   PR's state, and whether the test access point still reports to the
   controller. Say where things stand before you carry on.

### Delegating to sub-agents

- Hand read-heavy work to a separate agent: searching a large codebase,
  reading protocol captures or package docs, and reviewing a pull request
  with fresh eyes. It keeps the main session's context small.
- Give it a self-contained brief: what is known, the exact questions, where
  to look, and that it must only read.
- Check the findings you build on yourself before using them; a sub-agent's
  mistakes read as confidently as its facts.
- Keep changes to the lab (deploys, network changes, controller settings)
  and anything needing a sign-in in the main session, where the person you
  work with can see them.
