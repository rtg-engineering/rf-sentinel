# VLF/LF/MF Scanner Plugin

RF Sentinel plugin for scanning VLF, LF, MF, and medium-wave AM broadcast signals with an SDRplay receiver.

## Usage

```bash
cd rf_platform/plugins/am-broadcast  # from the repository root
python -m pip install -e .
am-broadcast scan --serial 1710022B20
lf-mf-scan scan --serial 1710022B20 --band 1khz-1mhz
```

Band presets:

- `vlf`: 3-30 kHz, 1 kHz steps
- `lf`: 30-300 kHz, 5 kHz steps
- `mf`: 300-3000 kHz, 10 kHz steps
- `am`: 530-1700 kHz, 10 kHz steps
- `1khz-1mhz`: 1-1000 kHz, 5 kHz steps
- `vlf-lf-mf`: 3-3000 kHz, 10 kHz steps

Default scan plan:

- AM broadcast band: 530 kHz to 1700 kHz
- Channel spacing: 10 kHz
- SDRplay driver: `driver=sdrplay`
- Native sample format: CS16

Useful options:

```bash
am-broadcast scan --top 20
am-broadcast scan --band vlf --sort freq
am-broadcast scan --band lf --top 30
am-broadcast scan --band mf --top 30
am-broadcast scan --band 1khz-1mhz --top 40
am-broadcast scan --band vlf-lf-mf --yes --top 50
am-broadcast scan --sort freq
am-broadcast scan --json
am-broadcast scan --csv
am-broadcast scan --show-driver-log
am-broadcast scan --start-khz 1 --stop-khz 1000 --step-khz 5
am-broadcast scan --antenna A --sample-rate-sps 250000 --bandwidth-hz 80000 --dwell-s 0.2
```

Scans with more than 400 channels require `--yes` so an accidental fine-step VLF/LF/MF sweep does not run for several minutes.

If the SDRplay Python module is installed outside your virtualenv, run from the same shell where `SoapySDRUtil --find='driver=sdrplay'` works. You can also set:

```bash
export SDRPLAY_SERIAL=1710022B20
```
