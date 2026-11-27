# Third-Party Notices

RF Sentinel is proprietary commercial software. Some plugins and dependencies
are distributed under separate open-source licenses.

## Bluetooth Classic SDR Stack

- Path: `rf_platform/plugins/bluetooth-classic`
- License: GPLv3
- Based on: [`bsnet/btsniffer`](https://github.com/bsnet/btsniffer)
- Notes: Distributed as a separate plugin/submodule. The plugin may be used,
  modified, and redistributed under GPLv3. This does not grant rights to the
  proprietary RF Sentinel application itself.

## MapLibre GL JS

- Files: `ui/frontend/vendor/maplibre-gl.js`, `ui/frontend/vendor/maplibre-gl.css` (v5.24.0, unmodified)
- License: BSD 3-Clause, https://github.com/maplibre/maplibre-gl-js/blob/v5.24.0/LICENSE.txt
- Used by: the fleet view (`/fleet`). Base-map tiles are fetched from Esri ArcGIS Online services at view time and
  carry their own attribution and terms.

## Other Dependencies

Runtime dependencies, SDR drivers, system tools, and protocol plugins may have
their own licenses. Review each dependency or plugin repository before
redistribution.

