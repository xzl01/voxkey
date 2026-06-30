# VoxKey ASR Service

This directory is the process boundary between the desktop UI and compute
backends.

The desktop shell should not import model runtimes directly. It should start a
local service and speak a stable protocol so CPU, GPU, and NPU backends can be
installed, updated, benchmarked, and removed independently.

Initial API targets:

- `GET /health`
- `GET /runtimes`
- `POST /models/download`
- `POST /transcribe`
- `POST /benchmark`
