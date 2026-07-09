# macOS-specific backend files

This directory holds files and dependencies that are unique to macOS
(e.g. a future `run.sh` launcher, launchd plist, or macOS-only audio/input
integrations for the voice daemon).

The shared, cross-platform backend lives one level up
(`../voice-daemon`, `../asr-service`, `../core`). Only macOS-exclusive
artifacts belong here so they stay physically isolated from Linux/Windows.
