# Live agent runs (public copies)

Reports of agent runs on real receivers, published with `rf-sentinel agent publish`. Each folder holds `run.json`,
`report.html` and `checksums.sha256`. Device identifiers are pseudonymised (`ble-01`, `cell-03`, consistent within a
report); advertised names, cell IDs, tracking-area codes, PCI numbers in labels, receiver serials and the recording
host's name are removed. `run.json` → `redaction.source_run_json_sha256` is the hash of the private original.

## RUN-691531415705: overnight standing watch, 2026-11-27

> Sweep for bluetooth trackers for 2 minutes and send an urgent alert when a new one appears. Then survey cell
> towers for 2 minutes and send an urgent alert if a 2G cell or an unknown network appears. Repeat 14 times, every
> 30 minutes.

- **Hardware:** two bladeRF 2.0 micro receivers (BLE) and a Quectel RM520N-GL modem without a SIM (cells), one site.
- **Ran unattended for 6.5 h:** 14 of 14 cycles started on schedule; 111 distinct devices; 10 alerts.
- **Cell survey:** ran in all 14 cycles. No 2G cell and no unknown network appeared.
- **BLE sweep:** ran in cycles 1–5. In cycles 6–14 it failed, because the IQ service process exited at about
  07:19 UTC without an error in its log (no Python traceback: a native-code crash is suspected,
  under investigation). The report records each failed step with its reason; the watch carried on with the steps
  that could run.
- **The "new tracker" alerts:** two per cycle in cycles 1–5, all for the same make of tracker. Trackers rotate
  their random address, so each new address counts as a new device: a known limitation (see `docs/agent.md`).

What this shows: an unattended mission running on real hardware for hours, reporting failures honestly. What it
does not show: detection rate, range, or decoder sensitivity, or behaviour at other sites.
