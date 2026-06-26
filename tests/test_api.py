"""Integration tests for the API server endpoints."""
from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
from fastapi.testclient import TestClient

from api_server import app, JOB_STATE, JOB_TASKS, process_translation_job
from srt_translator.config import TranslatorConfig
from srt_translator.llm_client import LLMCallError
from srt_translator.streaming_pipeline import StreamingAgentResult


@pytest.fixture
def client():
    """Create a TestClient for the FastAPI app."""
    return TestClient(app)


class TestHealthEndpoint:
    """Test the /api/health endpoint."""

    def test_health_check(self, client):
        """Test health check returns ok."""
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestTranslateEndpoint:
    """Test the /api/translate endpoint."""

    def test_translate_no_file(self, client):
        """Test translate without file returns 422."""
        response = client.post("/api/translate")
        assert response.status_code == 422  # Validation error

    def test_translate_with_mock_file(self, client, tmp_path):
        """Test translate with a mock SRT file."""
        # Create a test SRT file
        srt_content = (
            "1\n00:00:01,000 --> 00:00:03,500\nHello world\n\n"
            "2\n00:00:04,000 --> 00:00:06,500\nHow are you?\n"
        )
        test_file = tmp_path / "test.srt"
        test_file.write_text(srt_content, encoding="utf-8")

        with patch("api_server.process_translation_job", new_callable=AsyncMock) as mock_process:
            with open(test_file, "rb") as f:
                response = client.post(
                    "/api/translate",
                    files={"file": ("test.srt", f, "application/octet-stream")},
                    data={
                        "api_key": "test-key",
                        "model_name": "test-model",
                        "save_merged_subtitles": "true",
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert "job_id" in data
            assert "expected_output" in data
            assert "expected_report_output" in data
            assert "expected_merged_output" in data
            assert Path(data["expected_merged_output"]).name == "merged_test.srt"
            assert mock_process.call_args.args[8] is True

            # Verify JOB_STATE was created
            job_id = data["job_id"]
            assert job_id in JOB_STATE
            assert JOB_STATE[job_id]["status"] == "pending"

            # Cleanup
            del JOB_STATE[job_id]
            JOB_TASKS.pop(job_id, None)

    def test_translate_concurrency_limit(self, client, tmp_path):
        """Test concurrency limit is enforced."""
        # Fill JOB_STATE with running jobs to hit the limit
        for i in range(6):  # MAX_CONCURRENT_JOBS is 5
            JOB_STATE[f"existing-{i}"] = {"status": "running"}

        srt_content = "1\n00:00:01,000 --> 00:00:03,500\nHello\n"
        test_file = tmp_path / "test.srt"
        test_file.write_text(srt_content, encoding="utf-8")

        with open(test_file, "rb") as f:
            response = client.post(
                "/api/translate",
                files={"file": ("test.srt", f, "application/octet-stream")},
                data={"api_key": "test-key"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "服务器繁忙" in data["error"]

        # Cleanup
        for i in range(6):
            JOB_STATE.pop(f"existing-{i}", None)

    def test_translate_requires_local_auth_when_token_is_configured(
        self, client, tmp_path, monkeypatch
    ):
        """Test translate rejects requests missing the sidecar auth token."""
        monkeypatch.setenv("NBL_SUBTITLE_API_TOKEN", "secret-token")
        test_file = tmp_path / "test.srt"
        test_file.write_text(
            "1\n00:00:01,000 --> 00:00:03,500\nHello\n",
            encoding="utf-8",
        )

        with open(test_file, "rb") as f:
            response = client.post(
                "/api/translate",
                files={"file": ("test.srt", f, "application/octet-stream")},
                data={"api_key": "test-key"},
            )

        assert response.status_code == 401

    def test_translate_rejects_upload_over_configured_size(
        self, client, tmp_path, monkeypatch
    ):
        """Test oversized uploads are rejected before a job is created."""
        monkeypatch.setenv("NBL_SUBTITLE_MAX_UPLOAD_BYTES", "20")
        test_file = tmp_path / "large.srt"
        test_file.write_text(
            "1\n00:00:01,000 --> 00:00:03,500\nThis line is too large\n",
            encoding="utf-8",
        )

        with open(test_file, "rb") as f:
            response = client.post(
                "/api/translate",
                files={"file": ("large.srt", f, "application/octet-stream")},
                data={"api_key": "test-key"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "文件过大" in data["error"]
        assert not any(
            state.get("logs", [{}])[0].get("text") == "- 已接收文件: large.srt"
            for state in JOB_STATE.values()
        )

    def test_translate_rejects_invalid_srt_content(self, client, tmp_path):
        """Test files with .srt names still need parseable SRT content."""
        test_file = tmp_path / "invalid.srt"
        test_file.write_text("not actually subtitles", encoding="utf-8")

        with open(test_file, "rb") as f:
            response = client.post(
                "/api/translate",
                files={"file": ("invalid.srt", f, "application/octet-stream")},
                data={"api_key": "test-key"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "No valid SRT entries found" in data["error"]

    def test_translate_chooses_non_overwriting_output_path(self, client, tmp_path):
        """Test output paths do not overwrite existing translated subtitles."""
        existing = tmp_path / "translated_test.srt"
        existing.write_text("keep me", encoding="utf-8")
        test_file = tmp_path / "test.srt"
        test_file.write_text(
            "1\n00:00:01,000 --> 00:00:03,500\nHello\n",
            encoding="utf-8",
        )

        with patch("api_server.process_translation_job", new_callable=AsyncMock):
            with open(test_file, "rb") as f:
                response = client.post(
                    "/api/translate",
                    files={"file": ("test.srt", f, "application/octet-stream")},
                    data={"api_key": "test-key", "save_path": str(tmp_path)},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert Path(data["expected_output"]).name == "translated_test_1.srt"

        job_id = data["job_id"]
        JOB_STATE.pop(job_id, None)
        JOB_TASKS.pop(job_id, None)

    def test_translate_uses_same_non_overwriting_suffix_for_merged_output(
        self, client, tmp_path
    ):
        """Test merged/report outputs share the chosen non-overwriting suffix."""
        (tmp_path / "merged_test.srt").write_text("keep me", encoding="utf-8")
        test_file = tmp_path / "test.srt"
        test_file.write_text(
            "1\n00:00:01,000 --> 00:00:03,500\nHello\n",
            encoding="utf-8",
        )

        with patch("api_server.process_translation_job", new_callable=AsyncMock):
            with open(test_file, "rb") as f:
                response = client.post(
                    "/api/translate",
                    files={"file": ("test.srt", f, "application/octet-stream")},
                    data={
                        "api_key": "test-key",
                        "save_path": str(tmp_path),
                        "save_merged_subtitles": "true",
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert Path(data["expected_output"]).name == "translated_test_1.srt"
        assert Path(data["expected_merged_output"]).name == "merged_test_1.srt"
        assert Path(data["expected_report_output"]).name == (
            "translated_test_1.agent-report.json"
        )

        job_id = data["job_id"]
        JOB_STATE.pop(job_id, None)
        JOB_TASKS.pop(job_id, None)


class TestStatusEndpoint:
    """Test the /api/status endpoint."""

    def test_status_nonexistent_job(self, client):
        """Test status for nonexistent job."""
        response = client.get("/api/status/nonexistent-id")
        assert response.status_code == 200
        assert response.json()["status"] == "error"

    def test_status_existing_job(self, client):
        """Test status for existing job."""
        job_id = "test-status-job"
        JOB_STATE[job_id] = {
            "status": "running",
            "progress_pct": 50,
            "logs": [{"text": "- 测试日志", "isError": False}],
            "error": None,
            "created_at": 1000.0,
            "completed_at": None,
        }

        response = client.get(f"/api/status/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["progress"] == 50
        assert len(data["logs"]) == 1

        # Cleanup
        del JOB_STATE[job_id]

    def test_status_requires_local_auth_when_token_is_configured(
        self, client, monkeypatch
    ):
        """Test status endpoint rejects missing local auth token."""
        monkeypatch.setenv("NBL_SUBTITLE_API_TOKEN", "secret-token")

        response = client.get("/api/status/nonexistent-id")

        assert response.status_code == 401


@pytest.mark.asyncio
async def test_process_translation_job_exposes_warning_completion(tmp_path):
    job_id = "warning-job"
    input_path = tmp_path / "input.srt"
    output_path = tmp_path / "translated.srt"
    input_path.write_text("input", encoding="utf-8")
    JOB_STATE[job_id] = {"status": "pending", "logs": [], "error": None}
    result = StreamingAgentResult(
        output_path=output_path,
        report_path=tmp_path / "translated.agent-report.json",
        block_count=2,
        translated_count=1,
        fallback_original_count=1,
        status="completed_with_warnings",
        successful_ratio=0.5,
    )
    config = TranslatorConfig(api_key="key", model_name="m", summary_model_name="m")

    with patch("api_server.StreamingAgentPipeline.run_file", new_callable=AsyncMock) as run:
        run.return_value = result
        await process_translation_job(job_id, input_path, output_path, None, config)

    assert JOB_STATE[job_id]["status"] == "completed_with_warnings"
    assert JOB_STATE[job_id]["stats"]["fallback_original_count"] == 1
    JOB_STATE.pop(job_id, None)


@pytest.mark.asyncio
async def test_process_translation_job_marks_task_level_failure_as_error(tmp_path):
    job_id = "failed-job"
    input_path = tmp_path / "input.srt"
    output_path = tmp_path / "translated.srt"
    input_path.write_text("input", encoding="utf-8")
    JOB_STATE[job_id] = {"status": "pending", "logs": [], "error": None}
    config = TranslatorConfig(api_key="key", model_name="m", summary_model_name="m")

    with patch("api_server.StreamingAgentPipeline.run_file", new_callable=AsyncMock) as run:
        run.side_effect = LLMCallError("authentication failed")
        await process_translation_job(job_id, input_path, output_path, None, config)

    assert JOB_STATE[job_id]["status"] == "error"
    assert "authentication failed" in JOB_STATE[job_id]["error"]
    JOB_STATE.pop(job_id, None)


class TestCancelEndpoint:
    """Test the /api/cancel endpoint."""

    def test_cancel_nonexistent_job(self, client):
        """Test cancel for nonexistent job."""
        response = client.post("/api/cancel/nonexistent-id")
        assert response.status_code == 200
        assert response.json()["status"] == "error"

    def test_cancel_running_job(self, client):
        """Test cancel for a running job."""
        job_id = "test-cancel-job"
        JOB_STATE[job_id] = {
            "status": "running",
            "progress_pct": 30,
            "logs": [],
            "error": None,
            "created_at": 1000.0,
            "completed_at": None,
        }

        # Create a mock task that is done (simulating completed cancellation)
        mock_task = MagicMock()
        mock_task.done.return_value = True  # Already done, so cancel() won't be called
        JOB_TASKS[job_id] = mock_task

        response = client.post(f"/api/cancel/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["message"] == "任务已取消。"
        assert JOB_STATE[job_id]["status"] == "cancelled"
        assert JOB_STATE[job_id]["completed_at"] is not None

        # Cleanup
        del JOB_STATE[job_id]
        del JOB_TASKS[job_id]

    def test_cancel_completed_job(self, client):
        """Test cancel for already completed job."""
        job_id = "test-completed-job"
        JOB_STATE[job_id] = {
            "status": "completed",
            "progress_pct": 100,
            "logs": [],
            "error": None,
            "created_at": 1000.0,
            "completed_at": 2000.0,
        }

        response = client.post(f"/api/cancel/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "无法取消" in data["error"]

        # Cleanup
        del JOB_STATE[job_id]

    def test_cancel_requires_local_auth_when_token_is_configured(
        self, client, monkeypatch
    ):
        """Test cancel endpoint rejects missing local auth token."""
        monkeypatch.setenv("NBL_SUBTITLE_API_TOKEN", "secret-token")

        response = client.post("/api/cancel/nonexistent-id")

        assert response.status_code == 401


def test_health_requires_local_auth_when_token_is_configured(client, monkeypatch):
    """Test health endpoint participates in the sidecar auth handshake."""
    monkeypatch.setenv("NBL_SUBTITLE_API_TOKEN", "secret-token")

    response = client.get("/api/health")

    assert response.status_code == 401
