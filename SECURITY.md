# Security Policy

## Project status

Open Omada Device Agent is experimental interoperability software. It opens
network connections to an Omada controller and intentionally implements a
subset of a device-management protocol. Treat it as lab software unless you
have reviewed the code and deployment model for your environment.

## Sensitive material

Do not commit or publish:

- Device Account passwords;
- controller/cloud authentication tokens or cookies;
- private keys or client certificates;
- `.env` and `.env.*` files (except `.env.example`);
- packet captures from networks you are not authorized to inspect;
- managed-state files (`.omada-agent-state-*.json`, `.omada-device-mac`)
  containing sensitive deployment metadata;
- logs made with `--dump-tx` or `--debug` (see below).

The agent's managed-state file is designed not to persist the Device Account
password, but it still contains controller identifiers and routing metadata and
should be treated as operational data.

## Logs

`--dump-tx` prints every ECSP message in full. Config messages from the
controller include Wi-Fi passwords and SNMP communities (and RADIUS secrets
when a portal or SSID uses RADIUS), so these logs must not be shared without
removing them. `--debug` can also include parts of these messages.

## Features that change the network

Some controller settings rewrite the access point's own network. They can
cut the AP off from the controller if they are wrong:

- **IP Settings** (`OMADA_CAP_IP_SETTINGS`, off by default) rewrites the LAN
  address. A changed address is rolled back after 3 minutes unless the agent
  reaches the controller from it.
- **Captive portal** (openNDS) intercepts traffic on its bridge. The agent
  refuses to run it on the bridge that carries the uplink; use a routed guest
  network (see [docs/captive-portal.md](docs/captive-portal.md)).
- **SNMP** writes communities and SNMPv3 credentials from the controller to
  `/etc/config/snmpd` on the AP.

The agent runs as root on the AP. Restrict who can log in to the AP (set a
root password or SSH keys) and who can manage the controller, since either
can reconfigure the device.

## TLS

Certificate verification is disabled by default because Omada management
endpoints commonly use private or self-signed certificates. This protects
interoperability but does not authenticate the controller certificate.

For environments with a trusted certificate chain, set:

```dotenv
OMADA_TLS_VERIFY=true
OMADA_TLS_CA_FILE=/path/to/ca.pem
```

## Reporting vulnerabilities

Report vulnerabilities privately through the repository's
[Security tab](https://github.com/grad1e/open-omada-device-agent/security/advisories/new)
("Report a vulnerability"). Do not open a public issue containing credentials,
exploit material against third-party systems, or private packet captures.
