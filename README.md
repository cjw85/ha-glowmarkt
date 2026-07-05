# ha-glowmarkt

Home Assistant custom integration for Glowmarkt/Bright smart-meter accounts in Great Britain.

This is a whitebox reimplementation of the `hildebrandglow_dcc` integration.
This integration can run alongside the original.

Communication with Glowmarkt is handled using the [documented API](https://docs.glowmarkt.com/GlowmarktAPIDataRetrievalDocumentationIndividualUserForBright.pdf).

## What it does

- Creates one canonical electricity meter device and one canonical gas meter device per Glow virtual entity.
- Exposes half-hour electricity import and export, and gas import, data.
- Exposes `Usage (today)` and `Cost (today)` for supported meters.
- Exposes `Export (today)` on the canonical electricity meter when Glow provides a direct `electricity.export` resource.
- Exposes tariff `Standing charge` and `Rate` entities, disabled by default.
- Imports canonical half-hourly historical data into Home Assistant.

## Installation

### HACS

Add this repository as a custom integration in HACS, then install `Glowmarkt`:

[![Open your Home Assistant instance and open a repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cjw85&repository=ha-glowmarkt)

If you add it manually, use:

- Owner: `cjw85`
- Repository: `ha-glowmarkt`
- Category: `Integration`

### Manual

Copy `custom_components/glowmarkt/` into your Home Assistant `config/custom_components/` directory and restart Home Assistant.

## Setup

After restart, add the integration in Home Assistant and search for `Glowmarkt`.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=glowmarkt)

Use your Bright/Glowmarkt email address and password.

The integration creates devices from the account structure returned by Glow.
Sensors may appear as unavailable for a few seconds on first setup while the initial refresh completes.

## Energy Dashboard

The integration backfills and refreshes canonical electricity import/export and gas usage statistics from Glow half-hour data, aggregated into hourly long-term statistics.
That is the intended Energy dashboard path for this fork.

The daily entities reset and are not suitable as direct Energy dashboard sources.

## Polling

The options flow lets you set:

- Daily refresh interval in minutes
- Tariff refresh interval in minutes

Values below 5 minutes are rejected. Polling faster than Glow updates its upstream data is not useful and just adds API load.

## Debugging

To capture setup and refresh logs, add this before adding or reloading the integration:

```yaml
logger:
  default: warning
  logs:
    custom_components.glowmarkt: debug
```

## Development

Use Python 3.12+ and install the development environment with `uv`:

```bash
uv sync --extra dev
```

The dev environment includes Home Assistant and runs the test suite against Home Assistant's pytest harness rather than local stubs.

Useful commands:

```bash
uv run pytest
uv run python -m tests.smoke_glow --help
uv run black custom_components tests
uv run isort custom_components tests
```

The smoke test is meant to verify live Glow access, canonical resource selection, and half-hour import/export history retrieval:

```bash
uv run python -m tests.smoke_glow -u you@example.com -p 'secret' --history-days 3 --show-all-resources
```

## Credits

This repo started as an attempt to add export data to the [jonandel/ha-hildebrand-dcc](https://github.com/jonandel/ha-hildebrandglow-dcc), but ended up as a complete reimplementation. 
Along the way the pyglowmarkt module was reimplemented, due to missing bits and bugs, from the [specification](https://docs.glowmarkt.com/GlowmarktAPIDataRetrievalDocumentationIndividualUserForBright.pdf).
