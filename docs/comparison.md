# How RF Sentinel compares

This compares RF Sentinel with two widely used passive wireless monitoring tools, **Kismet** and **Nzyme**. All three are passive, but they solve somewhat different problems. Use this page to decide which fits your need.

**Method.** Competitor facts come from each project's own public pages, read on **2026-11-25**; the sources are listed below. "Not stated" means we found no statement of that capability on the pages read. It does not mean the product cannot do it. RF Sentinel entries are marked **live** (documented with real radios), **replay** (demonstrated in the synthetic demo), or **planned**. A ✓ does not imply equal depth or independently benchmarked performance. Corrections are welcome: [contact RTG](https://rtgspectrum.com/contact/).

## At a glance

| | RF Sentinel | Kismet | Nzyme |
|---|---|---|---|
| **Best for** | Explaining what changed in a multi-protocol RF environment, with verifiable evidence | Broad passive capture across the widest range of radios and sensors | Continuous WiFi/Bluetooth/Ethernet threat detection for a known network |
| **Price** | Commercial license; evaluations and pilots quoted | Free, open source | Free, Server Side Public License (SSPL) |
| **Hardware** | Bring your own SDRs (bladeRF and HackRF used in RTG testing) | Bring your own; very broad hardware support | Bring your own; Linux-supported adapters |
| **Hardware-free demo** | ✓ one-command Docker replay | Replay of its own log files | Not stated |

## Capabilities

| Capability | RF Sentinel | Kismet | Nzyme |
|---|---|---|---|
| WiFi (802.11) | Plugin; **replay** (live per deployment) | ✓ | ✓ |
| Bluetooth / BLE | ✓ **live** (incl. Classic LAP/UAP via SDR) | ✓ | ✓ (BLE support newer) |
| Zigbee / 802.15.4 | ✓ **live** | ✓ | Not stated |
| Sub-GHz sensors / TPMS | ✓ **live** | ✓ (rtl_433) | Not stated |
| Other bands (FM, VLF/LF/MF, cellular awareness) | Plugins; **replay** | Cellular (modem/DIAG), ADS-B, AIS, LoRa, and more | Ethernet, drone Remote ID |
| Distributed sensors | **Planned** (multi-site fleet) | ✓ remote capture | Not stated on pages read |
| Baseline of known devices/networks | ✓ snapshot baselines | ✓ device baseline (added 2026) | ✓ expected APs/SSIDs, timelines |
| Alerts / intrusion detection | Anomaly triage in the dashboard | ✓ WIDS alerts, syslog/SIEM | ✓ detections, email/Splunk actions |
| Pattern-of-life (recurrence by day and cadence) | ✓ | Trend-based alerts | Timelines of changes |
| **Investigation report explaining each change** | ✓ Analyst | Not stated | Not stated |
| **Every report claim cites evidence, re-checked by code** | ✓ | Not stated | Not stated |
| **Competing explanations ranked by evidence (incl. sensor fault)** | ✓ | Not stated | Not stated |
| **Separates a failed sensor from departed devices** | ✓ | Not stated | Not stated |
| **Checksummed evidence bundles** | ✓ | Not stated | Not stated |
| Published, reproducible performance measurements | ✓ | Not stated | Not stated |
| Optional LLM-assisted analysis (local or cloud) | ✓ behind read-only tools | Not stated | Not stated |
| API | JSON status and Analyst endpoints | ✓ extensive REST API | Not stated on pages read |

## How to choose

- **Choose Kismet** for the widest hardware and protocol coverage from a free tool, especially for capture, wardriving, or WIDS. RF Sentinel does not try to match its breadth of hardware support.
- **Choose Nzyme** for continuous threat detection on a WiFi/Bluetooth network you own, with approved-network monitoring and alerting.
- **Choose RF Sentinel** when you need to explain what changed in an RF environment, with every conclusion tied to evidence an engineer can verify. Bluetooth Classic identity work and sub-GHz/802.15.4 coverage are part of the same picture, and reports can be audited and reproduced.

The tools can complement each other. RF Sentinel normalizes observations into one event model, and other capture tools can feed it through an integration.

## Sources (read 2026-11-25)

- Kismet: [home](https://www.kismetwireless.net/) (free and open source; Wi-Fi, Bluetooth, Zigbee, RF; remote capture; REST API) · [intro](https://www.kismetwireless.net/docs/readme/intro/kismet/) · [data sources](https://www.kismetwireless.net/docs/readme/datasources/) · [remote capture](https://www.kismetwireless.net/docs/readme/remotecap/remotecap/) · [alerts / WIDS](https://www.kismetwireless.net/docs/readme/alerts/alerts/) · [baseline](https://www.kismetwireless.net/docs/readme/configuring/baseline/)
- Nzyme: [home](https://www.nzyme.org/) (SSPL; WiFi, Bluetooth, Ethernet) · [wireless capabilities](https://www.nzyme.org/capabilities/wireless) · [Bluetooth docs](https://docs.nzyme.org/bluetooth/) · [alerting](https://docs.nzyme.org/configuration/alerting/)
- RF Sentinel: [validation](validation.md) · [Analyst](analyst.md) · [evaluation guide and protocol status](evaluation.md)
