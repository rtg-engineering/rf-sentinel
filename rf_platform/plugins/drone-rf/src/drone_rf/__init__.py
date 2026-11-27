"""RF Sentinel adapter for an external drone RF detector.

The detector is not part of RF Sentinel. This package only:
- gives the detector a receiver that reads through RF Sentinel's IQ service (source.RfSentinelIqSource), so the
  detector and RF Sentinel never open the same radio;
- runs the detector (shim) and turns its events into RF Sentinel detections (events.to_detection).
"""
