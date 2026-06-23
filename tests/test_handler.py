import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

if "runpod" not in sys.modules:
    runpod_module = types.ModuleType("runpod")
    runpod_module.serverless = types.SimpleNamespace(start=lambda config: None)
    sys.modules["runpod"] = runpod_module

if "websocket" not in sys.modules:
    websocket_module = types.ModuleType("websocket")

    class WebSocketException(Exception):
        pass

    class WebSocketTimeoutException(WebSocketException):
        pass

    class WebSocketConnectionClosedException(WebSocketException):
        pass

    websocket_module.WebSocket = MagicMock
    websocket_module.WebSocketException = WebSocketException
    websocket_module.WebSocketTimeoutException = WebSocketTimeoutException
    websocket_module.WebSocketConnectionClosedException = (
        WebSocketConnectionClosedException
    )
    websocket_module.enableTrace = lambda enabled: None
    sys.modules["websocket"] = websocket_module

import handler


class TestRunpodWorkerComfy(unittest.TestCase):
    def test_valid_input_with_workflow_only(self):
        input_data = {"workflow": {"key": "value"}}
        validated_data, error = handler.validate_input(input_data)
        self.assertIsNone(error)
        self.assertEqual(
            validated_data,
            {
                "workflow": {"key": "value"},
                "comfy_org_api_key": None,
            },
        )

    def test_input_rejects_images(self):
        input_data = {
            "workflow": {"key": "value"},
            "images": [{"name": "image1.png", "image": "base64string"}],
        }
        validated_data, error = handler.validate_input(input_data)
        self.assertIsNone(validated_data)
        self.assertIn("'images' is no longer supported", error)

    def test_input_missing_workflow(self):
        input_data = {"images": [{"name": "image1.png", "image": "base64string"}]}
        validated_data, error = handler.validate_input(input_data)
        self.assertIsNotNone(error)
        self.assertEqual(error, "Missing 'workflow' parameter")

    def test_invalid_json_string_input(self):
        input_data = "invalid json"
        validated_data, error = handler.validate_input(input_data)
        self.assertIsNotNone(error)
        self.assertEqual(error, "Invalid JSON format in input")

    def test_valid_json_string_input(self):
        input_data = '{"workflow": {"key": "value"}}'
        validated_data, error = handler.validate_input(input_data)
        self.assertIsNone(error)
        self.assertEqual(
            validated_data,
            {
                "workflow": {"key": "value"},
                "comfy_org_api_key": None,
            },
        )

    def test_empty_input(self):
        input_data = None
        validated_data, error = handler.validate_input(input_data)
        self.assertIsNotNone(error)
        self.assertEqual(error, "Please provide input")

    def test_non_object_input(self):
        input_data = ["not", "an", "object"]
        validated_data, error = handler.validate_input(input_data)
        self.assertIsNone(validated_data)
        self.assertEqual(error, "Input must be a JSON object")

    @patch("handler.requests.get")
    def test_check_server_server_up(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        result = handler.check_server("http://127.0.0.1:8188", 1, 50)
        self.assertTrue(result)

    @patch("handler.requests.get")
    def test_check_server_server_down(self, mock_get):
        mock_get.side_effect = handler.requests.RequestException()
        result = handler.check_server("http://127.0.0.1:8188", 1, 50)
        self.assertFalse(result)

    @patch("handler.requests.post")
    def test_queue_prompt(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"prompt_id": "123"}
        mock_post.return_value = mock_response

        result = handler.queue_workflow({"prompt": "test"}, "client-123")

        self.assertEqual(result, {"prompt_id": "123"})

    def test_collect_r2_input_paths_and_key_mapping(self):
        workflow = {
            "1": {
                "inputs": {
                    "image": "/tmp/r2/inputs/a/b.png",
                    "duplicate": "/tmp/r2/inputs/a/b.png",
                    "output": "/tmp/r2/outputs/results/final.png",
                }
            }
        }

        paths = handler.collect_r2_input_paths(workflow)

        self.assertEqual(paths, ["/tmp/r2/inputs/a/b.png"])
        self.assertEqual(
            handler.local_r2_path_to_key(paths[0], handler.R2_INPUTS_DIR),
            "a/b.png",
        )

    def test_collect_r2_input_paths_rejects_parent_reference(self):
        workflow = {"1": {"inputs": {"image": "/tmp/r2/inputs/../bad.png"}}}

        with self.assertRaisesRegex(ValueError, "must not contain"):
            handler.collect_r2_input_paths(workflow)

    def test_collect_r2_input_paths_rejects_other_r2_path(self):
        workflow = {"1": {"inputs": {"image": "/tmp/r2/other/file.png"}}}

        with self.assertRaisesRegex(ValueError, "must start"):
            handler.collect_r2_input_paths(workflow)

    def test_collect_r2_input_paths_rejects_output_escape(self):
        workflow = {"1": {"inputs": {"output": "/tmp/r2/outputs/../bad.png"}}}

        with self.assertRaisesRegex(ValueError, "must not contain"):
            handler.collect_r2_input_paths(workflow)

    def test_collect_r2_input_paths_rejects_relative_parent_reference(self):
        workflow = {"1": {"inputs": {"filename_prefix": "../bad"}}}

        with self.assertRaisesRegex(ValueError, "path-like values"):
            handler.collect_r2_input_paths(workflow)

    def test_get_r2_config_reports_missing_variable_names_only(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "R2_ACCESS_KEY_ID"):
                handler.get_r2_config()

    def test_download_r2_inputs_creates_parent_dirs_and_downloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inputs_dir = os.path.join(tmpdir, "inputs")
            local_path = os.path.join(inputs_dir, "my", "files", "test.jpg")
            r2_client = MagicMock()

            with patch.object(handler, "R2_INPUTS_DIR", inputs_dir):
                handler.download_r2_inputs(r2_client, "bucket-name", [local_path])

            self.assertTrue(os.path.isdir(os.path.dirname(local_path)))
            r2_client.download_file.assert_called_once_with(
                "bucket-name", "my/files/test.jpg", local_path
            )

    def test_upload_r2_outputs_uploads_files_and_skips_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs_dir = os.path.join(tmpdir, "outputs")
            nested_dir = os.path.join(outputs_dir, "my", "files")
            os.makedirs(nested_dir)
            output_file = os.path.join(nested_dir, "test.jpg")
            with open(output_file, "wb") as f:
                f.write(b"test")

            symlink_path = os.path.join(outputs_dir, "linked.jpg")
            try:
                os.symlink(output_file, symlink_path)
            except OSError:
                pass

            r2_client = MagicMock()
            with patch.object(handler, "R2_OUTPUTS_DIR", outputs_dir):
                uploaded_count = handler.upload_r2_outputs(r2_client, "bucket-name")

            self.assertEqual(uploaded_count, 1)
            r2_client.upload_file.assert_called_once_with(
                output_file, "bucket-name", "my/files/test.jpg"
            )

    @patch("handler.cleanup_r2_workspace")
    @patch("handler.upload_r2_outputs")
    @patch("handler.download_r2_inputs")
    @patch("handler.reset_r2_workspace")
    @patch("handler.create_r2_client")
    @patch("handler.get_r2_config")
    @patch("handler.queue_workflow")
    @patch("handler.check_server")
    @patch("handler.websocket.WebSocket")
    def test_handler_success_runs_r2_steps_and_returns_status(
        self,
        mock_websocket_cls,
        mock_check_server,
        mock_queue_workflow,
        mock_get_r2_config,
        mock_create_r2_client,
        mock_reset_r2_workspace,
        mock_download_r2_inputs,
        mock_upload_r2_outputs,
        mock_cleanup_r2_workspace,
    ):
        events = []
        r2_client = MagicMock()
        mock_get_r2_config.return_value = {"R2_BUCKET": "bucket-name"}
        mock_create_r2_client.return_value = r2_client
        mock_reset_r2_workspace.side_effect = lambda: events.append("reset")
        mock_download_r2_inputs.side_effect = lambda *args: events.append("download")
        mock_upload_r2_outputs.side_effect = lambda *args: events.append("upload")
        mock_cleanup_r2_workspace.side_effect = lambda: events.append("cleanup")
        mock_check_server.return_value = True

        def queue_side_effect(*args, **kwargs):
            events.append("execute")
            return {"prompt_id": "prompt-123"}

        mock_queue_workflow.side_effect = queue_side_effect

        mock_ws = MagicMock()
        mock_ws.recv.return_value = json.dumps(
            {
                "type": "executing",
                "data": {"node": None, "prompt_id": "prompt-123"},
            }
        )
        mock_ws.connected = True
        mock_websocket_cls.return_value = mock_ws

        result = handler.handler({"id": "job-123", "input": {"workflow": {}}})

        self.assertEqual(result, {"status": "success"})
        self.assertEqual(events, ["reset", "download", "execute", "upload", "cleanup"])
        mock_download_r2_inputs.assert_called_once_with(r2_client, "bucket-name", [])
        mock_upload_r2_outputs.assert_called_once_with(r2_client, "bucket-name")

    @patch("handler.cleanup_r2_workspace")
    @patch("handler.download_r2_inputs")
    @patch("handler.reset_r2_workspace")
    @patch("handler.create_r2_client")
    @patch("handler.get_r2_config")
    def test_handler_cleans_up_when_r2_download_fails(
        self,
        mock_get_r2_config,
        mock_create_r2_client,
        mock_reset_r2_workspace,
        mock_download_r2_inputs,
        mock_cleanup_r2_workspace,
    ):
        mock_get_r2_config.return_value = {"R2_BUCKET": "bucket-name"}
        mock_create_r2_client.return_value = MagicMock()
        mock_download_r2_inputs.side_effect = ValueError("R2 download failed")

        result = handler.handler(
            {
                "id": "job-123",
                "input": {
                    "workflow": {
                        "1": {"inputs": {"image": "/tmp/r2/inputs/a/b.png"}}
                    }
                },
            }
        )

        self.assertEqual(result, {"error": "R2 download failed"})
        mock_reset_r2_workspace.assert_called_once()
        mock_cleanup_r2_workspace.assert_called_once()

    def test_start_sh_uses_r2_output_directory(self):
        start_sh_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "src", "start.sh")
        )
        with open(start_sh_path, "r") as f:
            contents = f.read()

        self.assertIn("mkdir -p /tmp/r2/outputs", contents)
        self.assertIn(
            'COMFY_OUTPUTS_DIR_OPTION="--output-directory /tmp/r2/outputs"',
            contents,
        )
        self.assertNotIn("--output-directory /comfyui/data/outputs", contents)
