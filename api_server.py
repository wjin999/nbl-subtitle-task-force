import os
import sys
import uuid
import shutil
import time
import asyncio
import hmac
import logging
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Dict
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# 基于 __file__ 的绝对路径，不依赖 cwd
_current_dir = os.path.dirname(os.path.abspath(__file__))
_src_path = os.path.join(_current_dir, "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from srt_translator.config import TranslatorConfig
from srt_translator.glossary import load_glossary_from_string
from srt_translator.llm_client import create_client
from srt_translator.parser import validate_srt_file
from srt_translator.streaming_pipeline import StreamingAgentPipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动逻辑
    asyncio.create_task(cleanup_expired_jobs())
    asyncio.create_task(cleanup_workspace())
    yield  # 应用运行中
    # 关闭逻辑（可选）


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api_server")

app = FastAPI(title="NBL Subtitle Task Force API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",        # Tauri 开发
        "http://127.0.0.1:1420",        # Tauri 开发（IP 访问）
        "http://tauri.localhost",       # Tauri 生产
        "tauri://localhost",            # Tauri 生产
        "https://tauri.localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _resolve_work_dir() -> Path:
    """Pick a writable runtime directory for uploaded temporary subtitles."""
    candidates = []
    if os.environ.get("NBL_SUBTITLE_WORK_DIR"):
        candidates.append(Path(os.environ["NBL_SUBTITLE_WORK_DIR"]))
    candidates.extend([
        Path(_current_dir) / ".runtime_workspace",
        Path(tempfile.gettempdir()) / "NBLSubtitleTaskForce" / "workspace",
    ])
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except Exception as exc:
            logger.warning("Runtime workspace unavailable %s: %s", candidate, exc)
    raise RuntimeError("No writable runtime workspace available")


WORK_DIR = _resolve_work_dir()

JOB_STATE: Dict[str, dict] = {}
JOB_TASKS: Dict[str, asyncio.Task] = {}  # 跟踪后台 asyncio 任务，用于真实取消
_concurrency_lock = asyncio.Lock()      # 并发控制锁
JOB_CLEANUP_INTERVAL = 300  # 每 5 分钟检查一次过期任务
JOB_RETENTION_SECONDS = 600  # 完成任务保留 10 分钟后清理
MAX_LOGS = 200  # 每个任务最大日志条数
MAX_CONCURRENT_JOBS = 5  # 最大并发翻译任务数
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class UploadTooLargeError(ValueError):
    """Raised when an uploaded subtitle exceeds the configured size limit."""


def _expected_auth_token() -> str:
    """Read the current local API token, if the sidecar configured one."""
    return os.environ.get("NBL_SUBTITLE_API_TOKEN", "").strip()


def require_local_auth(
    x_nbl_subtitle_token: Optional[str] = Header(default=None),
) -> None:
    """Protect local API endpoints when the Tauri sidecar supplies a token."""
    expected = _expected_auth_token()
    if expected and not hmac.compare_digest(x_nbl_subtitle_token or "", expected):
        raise HTTPException(status_code=401, detail="本地后台认证失败")


def _max_upload_bytes() -> int:
    raw = os.environ.get("NBL_SUBTITLE_MAX_UPLOAD_BYTES", "")
    try:
        value = int(raw) if raw else DEFAULT_MAX_UPLOAD_BYTES
    except ValueError:
        value = DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


def _copy_upload_with_limit(
    file: UploadFile,
    destination: Path,
    max_size_bytes: int,
) -> int:
    """Copy an upload to disk while enforcing a hard byte limit."""
    total = 0
    try:
        with destination.open("wb") as buffer:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size_bytes:
                    raise UploadTooLargeError(
                        f"文件过大：最大允许 {max_size_bytes / 1024 / 1024:.1f}MB"
                    )
                buffer.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return total


def _output_bundle_paths(
    output_dir: Path,
    safe_name: str,
    save_merged_subtitles: bool,
) -> tuple[Path, Path, Path]:
    """Choose output, merged, and report paths that do not overwrite files."""
    source_name = Path(safe_name)
    for index in range(1, 10_000):
        suffix = "" if index == 1 else f"_{index - 1}"
        output_path = output_dir / f"translated_{source_name.stem}{suffix}{source_name.suffix}"
        merged_output_path = output_dir / f"merged_{source_name.stem}{suffix}{source_name.suffix}"
        report_path = output_path.with_name(f"{output_path.stem}.agent-report.json")
        candidates = [output_path, report_path]
        if save_merged_subtitles:
            candidates.append(merged_output_path)
        if all(not path.exists() for path in candidates):
            return output_path, merged_output_path, report_path
    raise RuntimeError(f"无法生成不覆盖已有文件的输出路径: {output_dir / safe_name}")


def log_msg(job_id: str, msg: str, is_error: bool = False):
    if job_id in JOB_STATE:
        prefix = "[错误] " if is_error else "- "
        logs = JOB_STATE[job_id]["logs"]
        logs.append({"text": prefix + msg, "isError": is_error})
        # 限制日志数量，保留最新的 MAX_LOGS 条
        if len(logs) > MAX_LOGS:
            # 保留开头和结尾的重要日志
            logs[:] = logs[:10] + logs[-(MAX_LOGS - 10):]


def set_job_stage(job_id: str, stage: str) -> None:
    if job_id in JOB_STATE:
        JOB_STATE[job_id]["stage"] = stage


async def cleanup_expired_jobs():
    """定期清理过期的 JOB_STATE 条目。"""
    while True:
        try:
            await asyncio.sleep(JOB_CLEANUP_INTERVAL)
            now = time.time()
            expired_ids = [
                job_id for job_id, state in JOB_STATE.items()
                if state.get("completed_at") is not None
                and now - state["completed_at"] > JOB_RETENTION_SECONDS
            ]
            for job_id in expired_ids:
                try:
                    # 同时清理任务跟踪
                    JOB_TASKS.pop(job_id, None)
                    del JOB_STATE[job_id]
                    logger.info(f"Cleaned up expired job: {job_id}")
                except Exception as e:
                    logger.warning(f"Error cleaning up job {job_id}: {e}")
        except Exception as e:
            logger.error(f"Error in cleanup_expired_jobs: {e}")
            await asyncio.sleep(60)  # 避免快速重试


async def cleanup_workspace():
    """定期清理工作目录中超过 1 小时的临时文件。"""
    while True:
        await asyncio.sleep(3600)  # 每小时检查一次
        now = time.time()
        try:
            for f in WORK_DIR.iterdir():
                if f.is_file():
                    age = now - f.stat().st_mtime
                    if age > 3600:  # 超过 1 小时删除
                        f.unlink()
                        logger.info(f"Cleaned up temp file: {f.name}")
        except Exception:
            pass


async def process_translation_job(
    job_id: str,
    input_path: Path, 
    output_path: Path, 
    merged_output_path: Optional[Path],
    config: TranslatorConfig,
    summary_prompt: Optional[str] = None,
    translation_prompt: Optional[str] = None,
    glossary_text: str = "",
    save_merged_subtitles: bool = False,
):
    spool_dir = WORK_DIR / f"{job_id}_spool"
    try:
        JOB_STATE[job_id]["status"] = "running"
        JOB_STATE[job_id]["progress_pct"] = 1
        client = create_client(config.api_key, timeout=config.request_timeout)
        glossary_obj = load_glossary_from_string(glossary_text)
        if glossary_obj:
            log_msg(job_id, "已载入自定义术语表。")

        report_path = output_path.with_name(
            f"{output_path.stem}{config.report_suffix}"
        )
        stage_ranges = {
            "reading": (1, 10),
            "planning": (10, 30),
            "translating": (30, 72),
            "reviewing": (72, 84),
            "auditing": (84, 95),
            "writing": (95, 100),
        }

        async def _progress(stage: str, pct: int):
            if JOB_STATE.get(job_id, {}).get("status") == "cancelled":
                raise asyncio.CancelledError()
            set_job_stage(job_id, stage)
            start, end = stage_ranges.get(stage, (1, 100))
            JOB_STATE[job_id]["progress_pct"] = start + int((end - start) * pct / 100)

        async def _log(stage: str, message: str, is_error: bool = False):
            log_msg(job_id, f"[{stage}] {message}", is_error=is_error)

        pipeline = StreamingAgentPipeline(config)
        result = await pipeline.run_file(
            input_path=input_path,
            output_path=output_path,
            report_path=report_path,
            glossary=glossary_obj,
            client=client,
            summary_prompt=summary_prompt,
            translation_prompt=translation_prompt,
            merged_output_path=merged_output_path if save_merged_subtitles else None,
            spool_dir=spool_dir,
            resume=False,
            on_progress=_progress,
            on_log=_log,
        )
        
        JOB_STATE[job_id]["progress_pct"] = 100
        JOB_STATE[job_id]["status"] = result.status
        JOB_STATE[job_id]["completed_at"] = time.time()
        JOB_STATE[job_id]["report_path"] = str(result.report_path) if result.report_path else None
        JOB_STATE[job_id]["stats"] = {
            "block_count": result.block_count,
            "translated_count": result.translated_count,
            "fallback_original_count": result.fallback_original_count,
            "successful_ratio": result.successful_ratio,
        }
        if result.status == "completed_with_warnings":
            log_msg(
                job_id,
                f"任务完成，但有 {result.fallback_original_count} 个字幕块保留原文；"
                f"请查看报告: {result.report_path}",
                is_error=True,
            )
        else:
            log_msg(job_id, f"全部任务完成！文件位置: {output_path}")
        
    except asyncio.CancelledError:
        # 任务被真正取消
        JOB_STATE[job_id]["status"] = "cancelled"
        JOB_STATE[job_id]["completed_at"] = time.time()
        log_msg(job_id, "任务已被取消。")
        raise  # 必须重新抛出以标记协程为已取消
    except Exception as e:
        JOB_STATE[job_id]["status"] = "error"
        JOB_STATE[job_id]["error"] = str(e)
        JOB_STATE[job_id]["completed_at"] = time.time()
        log_msg(job_id, str(e), is_error=True)
    finally:
        # 清理上传的临时文件
        try:
            if input_path.exists():
                input_path.unlink()
                logger.info(f"Cleaned up temp file: {input_path}")
        except Exception as e:
            logger.warning(f"Failed to clean up temp file {input_path}: {e}")
        try:
            if spool_dir.exists():
                shutil.rmtree(spool_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to clean up spool dir {spool_dir}: {e}")
        JOB_TASKS.pop(job_id, None)


@app.post("/api/translate")
async def translate_endpoint(
    file: UploadFile = File(...),
    api_key: str = Form(""),
    summary_model_name: str = Form("deepseek-v4-pro"),
    model_name: str = Form("deepseek-v4-pro"),
    summary_prompt: str = Form(None),
    translation_prompt: str = Form(None),
    glossary: str = Form(""),
    save_path: str = Form(""),
    max_output_tokens: int = Form(4096),
    request_timeout: float = Form(60.0),
    save_merged_subtitles: bool = Form(False),
    _auth: None = Depends(require_local_auth),
):
    # 使用锁保护并发检查，避免竞态条件
    async with _concurrency_lock:
        running_jobs = sum(
            1 for s in JOB_STATE.values()
            if s.get("status") in ("running", "pending")
        )
        if running_jobs >= MAX_CONCURRENT_JOBS:
            return {
                "status": "error",
                "error": f"服务器繁忙，当前已有 {running_jobs} 个任务在运行（最大并发: {MAX_CONCURRENT_JOBS}）。请稍后再试。"
            }
    
    safe_name = Path(file.filename or "input.srt").name
    if not safe_name.lower().endswith(".srt"):
        return {"status": "error", "error": "仅支持 .srt 字幕文件"}

    job_id = str(uuid.uuid4())
    input_path = WORK_DIR / f"{job_id}_{safe_name}"
    
    if save_path and save_path.strip():
        # 去除用户可能粘贴的引号（如 "D:\Downloads" 或 'D:\Downloads'）
        raw_path = save_path.strip().strip("\"'")
        out_dir = Path(raw_path)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"status": "error", "error": f"保存路径不可用: {e}"}
    else:
        # 跨平台获取桌面路径
        _home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME", "."))
        out_dir = _home / "Desktop"
        if not out_dir.exists():
            out_dir = _home

    output_path, merged_output_path, report_path = _output_bundle_paths(
        out_dir,
        safe_name,
        save_merged_subtitles,
    )
    
    try:
        _copy_upload_with_limit(file, input_path, _max_upload_bytes())
    except UploadTooLargeError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return {"status": "error", "error": f"上传文件保存失败: {exc}"}

    validation_error = validate_srt_file(input_path, max_size_bytes=_max_upload_bytes())
    if validation_error:
        input_path.unlink(missing_ok=True)
        return {"status": "error", "error": validation_error}
        
    config = TranslatorConfig(
        api_key=api_key or None,
        summary_model_name=summary_model_name,
        model_name=model_name,
        max_output_tokens=max_output_tokens,
        request_timeout=request_timeout,
    )
    
    JOB_STATE[job_id] = {
        "status": "pending", "progress_pct": 0,
        "stage": "pending",
        "logs": [{"text": f"- 已接收文件: {safe_name}", "isError": False}],
        "error": None,
        "report_path": str(report_path),
        "stats": None,
        "created_at": time.time(),
        "completed_at": None,
    }

    # 校验配置（如 api_key 为空时提前返回友好错误信息）
    error = config.validate()
    if error:
        JOB_STATE[job_id]["status"] = "error"
        JOB_STATE[job_id]["error"] = error
        JOB_STATE[job_id]["logs"].append({"text": f"[错误] {error}", "isError": True})
        JOB_STATE[job_id]["completed_at"] = time.time()
        # 清理已上传的临时文件
        if input_path.exists():
            input_path.unlink()
        return {"status": "error", "error": error, "job_id": job_id}

    # 使用 asyncio.create_task 创建可取消的任务，并保存引用
    task = asyncio.create_task(
        process_translation_job(
            job_id,
            input_path,
            output_path,
            merged_output_path,
            config,
            summary_prompt,
            translation_prompt,
            glossary,
            save_merged_subtitles,
        )
    )
    JOB_TASKS[job_id] = task
    
    response = {
        "status": "success",
        "job_id": job_id,
        "expected_output": str(output_path),
        "expected_report_output": str(report_path),
    }
    if save_merged_subtitles:
        response["expected_merged_output"] = str(merged_output_path)
    return response


@app.get("/api/status/{job_id}")
async def get_status(job_id: str, _auth: None = Depends(require_local_auth)):
    if job_id not in JOB_STATE: return {"status": "error", "error": "任务 ID 不存在"}
    state = JOB_STATE[job_id]
    return {
        "status": state["status"],
        "stage": state.get("stage", ""),
        "progress": state["progress_pct"],
        "error": state["error"],
        "logs": state["logs"],
        "report_path": state.get("report_path"),
        "stats": state.get("stats"),
    }


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str, _auth: None = Depends(require_local_auth)):
    """取消一个正在执行或等待中的翻译任务。"""
    if job_id not in JOB_STATE:
        return {"status": "error", "error": "任务 ID 不存在"}
    
    state = JOB_STATE[job_id]
    current_status = state.get("status")
    
    if current_status in ("completed", "completed_with_warnings", "error", "cancelled"):
        return {"status": "error", "error": f"任务已结束（状态: {current_status}），无法取消。"}
    
    # 立即标记为已取消，新的翻译请求会排除此任务
    # CancelledError 会异步传播到 process_translation_job 完成清理
    task = JOB_TASKS.get(job_id)
    if task and not task.done():
        JOB_STATE[job_id]["status"] = "cancelled"
        task.cancel()
        return {"status": "success", "job_id": job_id, "message": "取消信号已发送，正在停止任务..."}
    else:
        # 任务不存在或已完成，直接设为已取消
        JOB_STATE[job_id]["status"] = "cancelled"
        JOB_STATE[job_id]["completed_at"] = time.time()
        log_msg(job_id, "任务已被用户取消。")
        return {"status": "success", "job_id": job_id, "message": "任务已取消。"}


@app.get("/api/health")
async def health_check(_auth: None = Depends(require_local_auth)):
    """后端健康检查接口，供前端轮询判断服务是否就绪。"""
    return {"status": "ok"}


@app.get("/api/health/spacy")
async def spacy_health(_auth: None = Depends(require_local_auth)):
    """检查 spaCy 模型是否已加载。"""
    from srt_translator.merger import _nlp_model
    loaded = _nlp_model is not None
    return {
        "available": loaded,
        "message": "spaCy model loaded" if loaded else "spaCy model not initialized yet",
    }


if __name__ == "__main__":
    host = os.environ.get("NBL_SUBTITLE_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("NBL_SUBTITLE_PORT", "18770"))
    except ValueError:
        port = 18770
    uvicorn.run(app, host=host, port=port)
