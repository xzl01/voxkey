# Windows-specific backend files

This directory holds files and dependencies that are unique to Windows
(e.g. a future `run.bat` launcher, a Windows service definition, or
WASAPI/Windows-specific input integrations for the voice daemon).

The shared, cross-platform backend lives one level up
(`../voice-daemon`, `../asr-service`, `../core`). Only Windows-exclusive
artifacts belong here so they stay physically isolated from Linux/macOS.
