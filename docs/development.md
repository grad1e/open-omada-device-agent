# Development Guide

## Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Tests

```bash
pytest
```

The current tests cover:

- length-prefixed ECSP framing;
- key message type values;
- legacy authentication calculations;
- first-adoption and managed-reconnect body shapes;
- conservative capability advertisement;
- managed informs;
- absolute and incremental SET config version handling;
- state persistence without password material;
- managed rediscovery identity.

## Lint

```bash
ruff check .
```

## Run from source

```bash
cp .env.example .env
open-omada-agent
```

In `.env`, set at least `OMADA_CONTROLLER_HOST` and `OMADA_DEVICE_PASSWORD`,
and delete the example `OMADA_CONTROLLER_ID` and `OMADA_SITE_ID` lines; the
controller fills these in at adoption. A new device is adopted with `admin` /
`admin`; switch to the site Device Account password once it is Connected (see
[Device Account authentication](configuration.md#device-account-authentication)).
The password is never written to the managed-state file.

Use a dedicated lab controller. `--dump-tx` logs complete ECSP JSON, including
Wi-Fi passwords from the controller's config, so sanitize logs before sharing
them. To run on an access point, see
[Install on an OpenWrt access point](install-openwrt.md).

The legacy launcher `python agent.py` is kept for migration. Add `--debug` for
more log output.

## Boot service without the deploy script

`scripts/deploy-openwrt.sh` installs the boot service for you (see
[Install on an OpenWrt access point](install-openwrt.md)). To install it by
hand from the checkout that holds your `.env` (procd restarts the agent after
a crash):

```bash
cp scripts/openwrt/open-omada-agent.init /etc/init.d/open-omada-agent
chmod +x /etc/init.d/open-omada-agent
/etc/init.d/open-omada-agent enable
/etc/init.d/open-omada-agent start
```

Edit `AGENT_DIR` and `AGENT_ARGS` at the top of the script if your checkout is
not `/root/open-omada-device-agent`. The service starts the agent with
`--workdir "$AGENT_DIR"`, which loads `.env` and the managed state file from
that folder. Output goes to the system log; read it with
`logread -e open_omada`. Add `--debug` to `AGENT_ARGS` only while
troubleshooting. Restart with `/etc/init.d/open-omada-agent restart`.

## Adding a message family

A suggested implementation sequence is:

1. add or verify the numeric message type in `ecsp.py`;
2. capture a minimal request/response pair;
3. create a body validator/model;
4. implement a handler without over-acknowledging unsupported behavior;
5. add tests;
6. update `protocol-status.md`;
7. advertise the corresponding component only when appropriate.
