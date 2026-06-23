# Configuration

This document outlines the environment variables available for configuring the `worker-comfyui`.

## General Configuration

| Environment Variable | Description                                                                                                                                                                                                                  | Default |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `REFRESH_WORKER`     | When `true`, the worker pod will stop after each completed job to ensure a clean state for the next job. See the [RunPod documentation](https://docs.runpod.io/docs/handler-additional-controls#refresh-worker) for details. | `false` |
| `SERVE_API_LOCALLY`  | When `true`, enables a local HTTP server simulating the RunPod environment for development and testing. See the [Development Guide](development.md#local-api) for more details.                                              | `false` |
| `COMFY_ORG_API_KEY`  | Comfy.org API key to enable ComfyUI API Nodes. If set, it is sent with each workflow; clients can override per request via `input.comfy_org_api_key`.                                                                       | –       |

## Cloudflare R2 Configuration

These variables are required because runtime input and output files are transferred through Cloudflare R2 using the S3-compatible API.

| Environment Variable    | Description                                                                 | Default |
| ----------------------- | --------------------------------------------------------------------------- | ------- |
| `R2_ACCOUNT_ID`         | Cloudflare account ID for the R2 account.                                   | –       |
| `R2_ACCESS_KEY_ID`      | R2 access key ID. Treat as secret; do not print it in logs.                 | –       |
| `R2_SECRET_ACCESS_KEY`  | R2 secret access key. Treat as secret; do not print it in logs.             | –       |
| `R2_BUCKET`             | R2 bucket used as the temporary file transit store.                         | –       |
| `R2_ENDPOINT`           | R2 S3-compatible endpoint, for example `https://<account>.r2.cloudflarestorage.com`. | –       |
| `R2_REGION`             | R2 region. Cloudflare commonly uses `auto` for S3-compatible clients.       | –       |

Workflow file paths map to R2 object keys by removing the local prefix:

- `/tmp/r2/inputs/my/file.png` downloads from R2 key `my/file.png`.
- `/tmp/r2/outputs/my/file.png` uploads to R2 key `my/file.png`.
- Paths containing `..` or paths outside `/tmp/r2/inputs/` and `/tmp/r2/outputs/` are rejected.

## Logging Configuration

| Environment Variable   | Description                                                                                                                                                      | Default |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `COMFY_LOG_LEVEL`      | Controls ComfyUI's internal logging verbosity. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Use `DEBUG` for troubleshooting, `INFO` for production. | `DEBUG` |
| `NETWORK_VOLUME_DEBUG` | Enable detailed network volume diagnostics in worker logs. Useful for debugging model path issues. See [Network Volumes & Model Paths](network-volumes.md).      | `false` |

## Debugging Configuration

| Environment Variable           | Description                                                                                                            | Default |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------- | ------- |
| `WEBSOCKET_RECONNECT_ATTEMPTS` | Number of websocket reconnection attempts when connection drops during job execution.                                  | `5`     |
| `WEBSOCKET_RECONNECT_DELAY_S`  | Delay in seconds between websocket reconnection attempts.                                                              | `3`     |
| `WEBSOCKET_TRACE`              | Enable low-level websocket frame tracing for protocol debugging. Set to `true` only when diagnosing connection issues. | `false` |
