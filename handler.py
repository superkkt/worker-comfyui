import runpod
import json
import time
import os
import requests
import websocket
import uuid
import socket
import traceback
import logging
import shutil
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from network_volume import (
    is_network_volume_debug_enabled,
    run_network_volume_diagnostics,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = int(
    os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", 50)
)
# Maximum number of API check attempts (0 = no limit, poll while ComfyUI process is alive)
COMFY_API_AVAILABLE_MAX_RETRIES = int(
    os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", 0)
)
# Fallback retry limit when PID file is unavailable and retries=0
COMFY_API_FALLBACK_MAX_RETRIES = 500
# PID file written by start.sh so we can detect if ComfyUI has crashed
COMFY_PID_FILE = "/tmp/comfyui.pid"
# Websocket reconnection behaviour (can be overridden through environment variables)
# NOTE: more attempts and diagnostics improve debuggability whenever ComfyUI crashes mid-job.
#   • WEBSOCKET_RECONNECT_ATTEMPTS sets how many times we will try to reconnect.
#   • WEBSOCKET_RECONNECT_DELAY_S sets the sleep in seconds between attempts.
#
# If the respective env-vars are not supplied we fall back to sensible defaults ("5" and "3").
WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))

# Extra verbose websocket trace logs (set WEBSOCKET_TRACE=true to enable)
if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
    # This prints low-level frame information to stdout which is invaluable for diagnosing
    # protocol errors but can be noisy in production – therefore gated behind an env-var.
    websocket.enableTrace(True)

# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"
# Enforce a clean state after each job is done
# see https://docs.runpod.io/docs/handler-additional-controls#refresh-worker
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"

R2_REQUIRED_ENV_VARS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_ENDPOINT",
    "R2_REGION",
)
R2_ROOT_DIR = "/tmp/r2"
R2_INPUTS_DIR = "/tmp/r2/inputs"
R2_OUTPUTS_DIR = "/tmp/r2/outputs"

# ---------------------------------------------------------------------------
# Helper: quick reachability probe of ComfyUI HTTP endpoint (port 8188)
# ---------------------------------------------------------------------------


def _comfy_server_status():
    """Return a dictionary with basic reachability info for the ComfyUI HTTP server."""
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    """
    Attempts to reconnect to the WebSocket server after a disconnect.

    Args:
        ws_url (str): The WebSocket URL (including client_id).
        max_attempts (int): Maximum number of reconnection attempts.
        delay_s (int): Delay in seconds between attempts.
        initial_error (Exception): The error that triggered the reconnect attempt.

    Returns:
        websocket.WebSocket: The newly connected WebSocket object.

    Raises:
        websocket.WebSocketConnectionClosedException: If reconnection fails after all attempts.
    """
    print(
        f"worker-comfyui - Websocket connection closed unexpectedly: {initial_error}. Attempting to reconnect..."
    )
    last_reconnect_error = initial_error
    for attempt in range(max_attempts):
        # Log current server status before each reconnect attempt so that we can
        # see whether ComfyUI is still alive (HTTP port 8188 responding) even if
        # the websocket dropped. This is extremely useful to differentiate
        # between a network glitch and an outright ComfyUI crash/OOM-kill.
        srv_status = _comfy_server_status()
        if not srv_status["reachable"]:
            # If ComfyUI itself is down there is no point in retrying the websocket –
            # bail out immediately so the caller gets a clear "ComfyUI crashed" error.
            print(
                f"worker-comfyui - ComfyUI HTTP unreachable – aborting websocket reconnect: {srv_status.get('error', 'status '+str(srv_status.get('status_code')))}"
            )
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )

        # Otherwise we proceed with reconnect attempts while server is up
        print(
            f"worker-comfyui - Reconnect attempt {attempt + 1}/{max_attempts}... (ComfyUI HTTP reachable, status {srv_status.get('status_code')})"
        )
        try:
            # Need to create a new socket object for reconnect
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)  # Use existing ws_url
            print(f"worker-comfyui - Websocket reconnected successfully.")
            return new_ws  # Return the new connected socket
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconn_err:
            last_reconnect_error = reconn_err
            print(
                f"worker-comfyui - Reconnect attempt {attempt + 1} failed: {reconn_err}"
            )
            if attempt < max_attempts - 1:
                print(
                    f"worker-comfyui - Waiting {delay_s} seconds before next attempt..."
                )
                time.sleep(delay_s)
            else:
                print(f"worker-comfyui - Max reconnection attempts reached.")

    # If loop completes without returning, raise an exception
    print("worker-comfyui - Failed to reconnect websocket after connection closed.")
    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_reconnect_error}"
    )


def validate_input(job_input):
    """
    Validates the input for the handler function.

    Args:
        job_input (dict): The input data to validate.

    Returns:
        tuple: A tuple containing the validated data and an error message, if any.
               The structure is (validated_data, error_message).
    """
    # Validate if job_input is provided
    if job_input is None:
        return None, "Please provide input"

    # Check if input is a string and try to parse it as JSON
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    if not isinstance(job_input, dict):
        return None, "Input must be a JSON object"

    # Validate 'workflow' in input
    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    if "images" in job_input:
        return (
            None,
            "'images' is no longer supported. Store input files in R2 and "
            "reference them with /tmp/r2/inputs/... paths in the workflow.",
        )

    # Optional: API key for Comfy.org API Nodes, passed per-request
    comfy_org_api_key = job_input.get("comfy_org_api_key")

    # Return validated data and no error
    return {
        "workflow": workflow,
        "comfy_org_api_key": comfy_org_api_key,
    }, None


def get_r2_config():
    """Load R2 runtime configuration without exposing secret values."""
    missing_vars = [name for name in R2_REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing_vars:
        raise ValueError(
            "Missing required R2 environment variables: " + ", ".join(missing_vars)
        )

    return {name: os.environ[name] for name in R2_REQUIRED_ENV_VARS}


def create_r2_client(config):
    """Create a boto3 S3-compatible client for Cloudflare R2."""
    return boto3.client(
        service_name="s3",
        endpoint_url=config["R2_ENDPOINT"],
        aws_access_key_id=config["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=config["R2_SECRET_ACCESS_KEY"],
        region_name=config["R2_REGION"],
    )


def _iter_workflow_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_workflow_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_workflow_strings(item)


def _validate_local_r2_path(path, base_dir):
    if ".." in path:
        raise ValueError(f"R2 local paths must not contain '..': {path}")

    normalized_path = os.path.abspath(os.path.normpath(path))
    normalized_base = os.path.abspath(os.path.normpath(base_dir))
    if (
        normalized_path == normalized_base
        or os.path.commonpath([normalized_base, normalized_path]) != normalized_base
    ):
        raise ValueError(f"R2 local path must be under {base_dir}: {path}")

    return normalized_path


def collect_r2_input_paths(workflow):
    input_paths = set()
    for value in _iter_workflow_strings(workflow):
        if ".." in value and ("/" in value or "\\" in value):
            raise ValueError(
                f"Workflow path-like values must not contain '..': {value}"
            )

        if value.startswith(R2_INPUTS_DIR + "/"):
            input_paths.add(_validate_local_r2_path(value, R2_INPUTS_DIR))
        elif value.startswith(R2_OUTPUTS_DIR + "/"):
            _validate_local_r2_path(value, R2_OUTPUTS_DIR)
        elif value.startswith(R2_ROOT_DIR + "/"):
            raise ValueError(
                "R2 local paths must start with /tmp/r2/inputs/ or "
                f"/tmp/r2/outputs/: {value}"
            )

    return sorted(input_paths)


def _remove_path(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def reset_r2_workspace():
    for path in (R2_INPUTS_DIR, R2_OUTPUTS_DIR):
        _remove_path(path)
        os.makedirs(path, exist_ok=True)


def cleanup_r2_workspace():
    for path in (R2_INPUTS_DIR, R2_OUTPUTS_DIR):
        _remove_path(path)


def local_r2_path_to_key(path, base_dir):
    normalized_path = _validate_local_r2_path(path, base_dir)
    relative_path = os.path.relpath(normalized_path, os.path.abspath(base_dir))
    return relative_path.replace(os.sep, "/")


def _r2_error_message(action, key, exc):
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", "No message")
        return f"R2 {action} failed for key '{key}' ({code}: {message})"

    return f"R2 {action} failed for key '{key}' ({exc.__class__.__name__})"


def download_r2_inputs(r2_client, bucket, input_paths):
    for local_path in input_paths:
        key = local_r2_path_to_key(local_path, R2_INPUTS_DIR)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            print(f"worker-comfyui - Downloading R2 input: {key}")
            r2_client.download_file(bucket, key, local_path)
        except (BotoCoreError, ClientError, OSError) as exc:
            raise ValueError(_r2_error_message("download", key, exc)) from exc

    print(f"worker-comfyui - Downloaded {len(input_paths)} R2 input file(s).")


def upload_r2_outputs(r2_client, bucket):
    uploaded_count = 0
    if not os.path.isdir(R2_OUTPUTS_DIR):
        print("worker-comfyui - R2 output directory does not exist; nothing to upload.")
        return uploaded_count

    for root, dirs, files in os.walk(R2_OUTPUTS_DIR, followlinks=False):
        dirs[:] = [
            dirname
            for dirname in dirs
            if not os.path.islink(os.path.join(root, dirname))
        ]

        for filename in files:
            local_path = os.path.join(root, filename)
            if os.path.islink(local_path) or not os.path.isfile(local_path):
                continue

            key = local_r2_path_to_key(local_path, R2_OUTPUTS_DIR)
            try:
                print(f"worker-comfyui - Uploading R2 output: {key}")
                r2_client.upload_file(local_path, bucket, key)
            except (BotoCoreError, ClientError, OSError) as exc:
                raise ValueError(_r2_error_message("upload", key, exc)) from exc

            uploaded_count += 1

    print(f"worker-comfyui - Uploaded {uploaded_count} R2 output file(s).")
    return uploaded_count


def _get_comfyui_pid():
    """Read the ComfyUI process PID from the PID file written by start.sh."""
    try:
        with open(COMFY_PID_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_comfyui_process_alive():
    """Check whether the ComfyUI process is still running.

    Returns True if alive, False if dead, None if PID file not found.
    """
    pid = _get_comfyui_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it


def check_server(url, retries=0, delay=50):
    """
    Check if a server is reachable via HTTP GET request.

    When a PID file is available (written by start.sh), the function polls
    indefinitely while the ComfyUI process is alive and fails immediately
    when the process exits.  When no PID file is found it falls back to
    the retry limit for backward compatibility.

    Args:
        url (str): The URL to check.
        retries (int): Max attempts. 0 means unlimited (poll while process alive).
        delay (int): Time in milliseconds between retries.

    Returns:
        bool: True if the server is reachable, False otherwise.
    """
    print(f"worker-comfyui - Checking API server at {url}...")

    # Guard against zero/negative delay to avoid division by zero
    delay = max(1, delay)
    # How often to print a "still waiting" log (every ~10 seconds)
    log_every = max(1, int(10_000 / delay))
    attempt = 0

    while True:
        # --- Check if ComfyUI process is still alive ---
        process_status = _is_comfyui_process_alive()
        if process_status is False:
            print(
                "worker-comfyui - ComfyUI process has exited. "
                "Server will not become reachable."
            )
            return False

        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print(f"worker-comfyui - API is reachable")
                return True
        except requests.Timeout:
            pass
        except requests.RequestException:
            pass

        attempt += 1

        # If we can't track the process, enforce a retry limit to avoid
        # hanging forever when the PID file is never written
        fallback = retries if retries > 0 else COMFY_API_FALLBACK_MAX_RETRIES
        if process_status is None and attempt >= fallback:
            print(
                f"worker-comfyui - Failed to connect to server at {url} "
                f"after {fallback} attempts (no PID file found)."
            )
            return False

        if attempt % log_every == 0:
            elapsed_s = (attempt * delay) / 1000
            print(
                f"worker-comfyui - Still waiting for API server... "
                f"({elapsed_s:.0f}s elapsed, attempt {attempt})"
            )

        time.sleep(delay / 1000)


def get_available_models():
    """
    Get list of available models from ComfyUI

    Returns:
        dict: Dictionary containing available models by type
    """
    try:
        response = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        response.raise_for_status()
        object_info = response.json()

        # Extract available checkpoints from CheckpointLoaderSimple
        available_models = {}
        if "CheckpointLoaderSimple" in object_info:
            checkpoint_info = object_info["CheckpointLoaderSimple"]
            if "input" in checkpoint_info and "required" in checkpoint_info["input"]:
                ckpt_options = checkpoint_info["input"]["required"].get("ckpt_name")
                if ckpt_options and len(ckpt_options) > 0:
                    available_models["checkpoints"] = (
                        ckpt_options[0] if isinstance(ckpt_options[0], list) else []
                    )

        return available_models
    except Exception as e:
        print(f"worker-comfyui - Warning: Could not fetch available models: {e}")
        return {}


def queue_workflow(workflow, client_id, comfy_org_api_key=None):
    """
    Queue a workflow to be processed by ComfyUI

    Args:
        workflow (dict): A dictionary containing the workflow to be processed
        client_id (str): The client ID for the websocket connection
        comfy_org_api_key (str, optional): Comfy.org API key for API Nodes

    Returns:
        dict: The JSON response from ComfyUI after processing the workflow

    Raises:
        ValueError: If the workflow validation fails with detailed error information
    """
    # Include client_id in the prompt payload
    payload = {"prompt": workflow, "client_id": client_id}

    # Optionally inject Comfy.org API key for API Nodes.
    # Precedence: per-request key (argument) overrides environment variable.
    # Note: We use our consistent naming (comfy_org_api_key) but transform to
    # ComfyUI's expected format (api_key_comfy_org) when sending.
    key_from_env = os.environ.get("COMFY_ORG_API_KEY")
    effective_key = comfy_org_api_key if comfy_org_api_key else key_from_env
    if effective_key:
        payload["extra_data"] = {"api_key_comfy_org": effective_key}
    data = json.dumps(payload).encode("utf-8")

    # Use requests for consistency and timeout
    headers = {"Content-Type": "application/json"}
    response = requests.post(
        f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30
    )

    # Handle validation errors with detailed information
    if response.status_code == 400:
        print(f"worker-comfyui - ComfyUI returned 400. Response body: {response.text}")
        try:
            error_data = response.json()
            print(f"worker-comfyui - Parsed error data: {error_data}")

            # Try to extract meaningful error information
            error_message = "Workflow validation failed"
            error_details = []

            # ComfyUI seems to return different error formats, let's handle them all
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", error_message)
                    if error_info.get("type") == "prompt_outputs_failed_validation":
                        error_message = "Workflow validation failed"
                else:
                    error_message = str(error_info)

            # Check for node validation errors in the response
            if "node_errors" in error_data:
                for node_id, node_error in error_data["node_errors"].items():
                    if isinstance(node_error, dict):
                        for error_type, error_msg in node_error.items():
                            error_details.append(
                                f"Node {node_id} ({error_type}): {error_msg}"
                            )
                    else:
                        error_details.append(f"Node {node_id}: {node_error}")

            # Check if the error data itself contains validation info
            if error_data.get("type") == "prompt_outputs_failed_validation":
                error_message = error_data.get("message", "Workflow validation failed")
                # For this type of error, we need to parse the validation details from logs
                # Since ComfyUI doesn't seem to include detailed validation errors in the response
                # Let's provide a more helpful generic message
                available_models = get_available_models()
                if available_models.get("checkpoints"):
                    error_message += f"\n\nThis usually means a required model or parameter is not available."
                    error_message += f"\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                else:
                    error_message += "\n\nThis usually means a required model or parameter is not available."
                    error_message += "\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(error_message)

            # If we have specific validation errors, format them nicely
            if error_details:
                detailed_message = f"{error_message}:\n" + "\n".join(
                    f"• {detail}" for detail in error_details
                )

                # Try to provide helpful suggestions for common errors
                if any(
                    "not in list" in detail and "ckpt_name" in detail
                    for detail in error_details
                ):
                    available_models = get_available_models()
                    if available_models.get("checkpoints"):
                        detailed_message += f"\n\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                    else:
                        detailed_message += "\n\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(detailed_message)
            else:
                # Fallback to the raw response if we can't parse specific errors
                raise ValueError(f"{error_message}. Raw response: {response.text}")

        except (json.JSONDecodeError, KeyError) as e:
            # If we can't parse the error response, fall back to the raw text
            raise ValueError(
                f"ComfyUI validation failed (could not parse error response): {response.text}"
            )

    # For other HTTP errors, raise them normally
    response.raise_for_status()
    return response.json()


def handler(job):
    """
    Handles a job using ComfyUI via websockets for status monitoring.

    Args:
        job (dict): A dictionary containing job details and input parameters.

    Returns:
        dict: A dictionary containing either an error message or a success status.
    """
    # ---------------------------------------------------------------------------
    # Network Volume Diagnostics (opt-in via NETWORK_VOLUME_DEBUG=true)
    # ---------------------------------------------------------------------------
    if is_network_volume_debug_enabled():
        run_network_volume_diagnostics()

    ws = None
    errors = []

    try:
        job_input = job["input"]

        # Make sure that the input is valid
        validated_data, error_message = validate_input(job_input)
        if error_message:
            return {"error": error_message}

        workflow = validated_data["workflow"]
        input_paths = collect_r2_input_paths(workflow)
        r2_config = get_r2_config()
        r2_client = create_r2_client(r2_config)

        reset_r2_workspace()
        download_r2_inputs(r2_client, r2_config["R2_BUCKET"], input_paths)

        # Make sure that the ComfyUI HTTP API is available before proceeding
        if not check_server(
            f"http://{COMFY_HOST}/",
            COMFY_API_AVAILABLE_MAX_RETRIES,
            COMFY_API_AVAILABLE_INTERVAL_MS,
        ):
            return {
                "error": f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."
            }

        client_id = str(uuid.uuid4())
        prompt_id = None

        # Establish WebSocket connection
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        print(f"worker-comfyui - Connecting to websocket: {ws_url}")
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        print(f"worker-comfyui - Websocket connected")

        # Queue the workflow
        try:
            # Pass per-request API key if provided in input
            queued_workflow = queue_workflow(
                workflow,
                client_id,
                comfy_org_api_key=validated_data.get("comfy_org_api_key"),
            )
            prompt_id = queued_workflow.get("prompt_id")
            if not prompt_id:
                raise ValueError(
                    f"Missing 'prompt_id' in queue response: {queued_workflow}"
                )
            print(f"worker-comfyui - Queued workflow with ID: {prompt_id}")
        except requests.RequestException as e:
            print(f"worker-comfyui - Error queuing workflow: {e}")
            raise ValueError(f"Error queuing workflow: {e}")
        except Exception as e:
            print(f"worker-comfyui - Unexpected error queuing workflow: {e}")
            # For ValueError exceptions from queue_workflow, pass through the original message
            if isinstance(e, ValueError):
                raise e
            else:
                raise ValueError(f"Unexpected error queuing workflow: {e}")

        # Wait for execution completion via WebSocket
        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        execution_done = False
        while True:
            try:
                out = ws.recv()
                if isinstance(out, str):
                    message = json.loads(out)
                    if message.get("type") == "status":
                        status_data = message.get("data", {}).get("status", {})
                        print(
                            f"worker-comfyui - Status update: {status_data.get('exec_info', {}).get('queue_remaining', 'N/A')} items remaining in queue"
                        )
                    elif message.get("type") == "executing":
                        data = message.get("data", {})
                        if (
                            data.get("node") is None
                            and data.get("prompt_id") == prompt_id
                        ):
                            print(
                                f"worker-comfyui - Execution finished for prompt {prompt_id}"
                            )
                            execution_done = True
                            break
                    elif message.get("type") == "execution_error":
                        data = message.get("data", {})
                        if data.get("prompt_id") == prompt_id:
                            error_details = f"Node Type: {data.get('node_type')}, Node ID: {data.get('node_id')}, Message: {data.get('exception_message')}"
                            print(
                                f"worker-comfyui - Execution error received: {error_details}"
                            )
                            errors.append(f"Workflow execution error: {error_details}")
                            break
                else:
                    continue
            except websocket.WebSocketTimeoutException:
                print(f"worker-comfyui - Websocket receive timed out. Still waiting...")
                continue
            except websocket.WebSocketConnectionClosedException as closed_err:
                try:
                    # Attempt to reconnect
                    ws = _attempt_websocket_reconnect(
                        ws_url,
                        WEBSOCKET_RECONNECT_ATTEMPTS,
                        WEBSOCKET_RECONNECT_DELAY_S,
                        closed_err,
                    )

                    print(
                        "worker-comfyui - Resuming message listening after successful reconnect."
                    )
                    continue
                except (
                    websocket.WebSocketConnectionClosedException
                ) as reconn_failed_err:
                    # If _attempt_websocket_reconnect fails, it raises this exception
                    # Let this exception propagate to the outer handler's except block
                    raise reconn_failed_err

            except json.JSONDecodeError:
                print(f"worker-comfyui - Received invalid JSON message via websocket.")

        if not execution_done and not errors:
            raise ValueError(
                "Workflow monitoring loop exited without confirmation of completion or error."
            )

        if errors:
            print(f"worker-comfyui - Job failed with errors: {errors}")
            return {
                "error": "Job processing failed",
                "details": errors,
            }

        upload_r2_outputs(r2_client, r2_config["R2_BUCKET"])

    except websocket.WebSocketException as e:
        print(f"worker-comfyui - WebSocket Error: {e}")
        print(traceback.format_exc())
        return {"error": f"WebSocket communication error: {e}"}
    except requests.RequestException as e:
        print(f"worker-comfyui - HTTP Request Error: {e}")
        print(traceback.format_exc())
        return {"error": f"HTTP communication error with ComfyUI: {e}"}
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        return {"error": str(e)}
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {e}"}
    finally:
        if ws and ws.connected:
            print(f"worker-comfyui - Closing websocket connection.")
            ws.close()
        cleanup_r2_workspace()

    print("worker-comfyui - Job completed successfully.")
    return {"status": "success"}


if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})
