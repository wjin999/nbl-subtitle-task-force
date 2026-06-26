import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";
import { invoke } from "@tauri-apps/api/core";

export const BACKEND_HOST = "127.0.0.1";
const BACKEND_TOKEN_HEADER = "X-NBL-Subtitle-Token";
const POLL_INTERVAL = 2500;
const BACKEND_READY_ATTEMPTS = 120;

type LogEntry = { text: string; isError: boolean };
type BackendConfig = { host: string; port: number; token: string };

async function getBackendConfig(): Promise<BackendConfig> {
  try {
    return await invoke<BackendConfig>("get_backend_config");
  } catch {
    throw new Error("无法获取本地后台配置，请从 Tauri 桌面应用启动");
  }
}

function backendBase(config: BackendConfig): string {
  return `http://${config.host}:${config.port}`;
}

function authHeaders(config: BackendConfig): Record<string, string> {
  return config.token ? { [BACKEND_TOKEN_HEADER]: config.token } : {};
}

const WORKFLOW_STAGES = [
  "导入",
  "拆分",
  "分析",
  "翻译",
  "复审",
  "输出",
];

function clampProgress(value: number): number {
  return Math.min(100, Math.max(0, value));
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function getActiveStage(progress: number, isWorking: boolean, hasFile: boolean): number {
  if (!hasFile) return -1;
  if (progress >= 100) return WORKFLOW_STAGES.length - 1;
  if (!isWorking && progress === 0) return 0;
  if (progress < 8) return 1;
  return Math.min(WORKFLOW_STAGES.length - 1, Math.floor(progress / 20) + 1);
}

function stageClass(index: number, activeStage: number, progress: number, isWorking: boolean, isError: boolean): string {
  if (isError && index === activeStage) return "is-error";
  if (!isError && progress >= 100) return "is-complete";
  if (index < activeStage && (isWorking || progress > 0)) return "is-complete";
  if (index === activeStage) return "is-active";
  return "is-pending";
}

interface Props {
  file: File | null;
  setFile: (f: File | null) => void;
  isWorking: boolean;
  setIsWorking: (v: boolean) => void;
  progress: number;
  setProgress: (v: number) => void;
  status: string;
  setStatus: (v: string) => void;
  isError: boolean;
  setIsError: (v: boolean) => void;
  logs: LogEntry[];
  setLogs: Dispatch<SetStateAction<LogEntry[]>>;
  apiKey: string;
  sumModel: string;
  transModel: string;
  sumPrompt: string;
  transPrompt: string;
  savePath: string;
  glossary: string;
  maxOutputTokens: number;
  requestTimeout: number;
  saveMergedSubtitles: boolean;
}

export default function TranslationView(props: Props) {
  const {
    file, setFile, isWorking, setIsWorking, progress, setProgress,
    status, setStatus, isError, setIsError, logs, setLogs,
    apiKey, sumModel, transModel,
    sumPrompt, transPrompt, savePath, glossary,
    maxOutputTokens, requestTimeout,
    saveMergedSubtitles,
  } = props;

  const logsContainerRef = useRef<HTMLDivElement>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const jobIdRef = useRef<string | null>(null);
  const pollingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const backendConfigRef = useRef<BackendConfig | null>(null);

  const stopPolling = () => {
    if (pollingTimerRef.current !== null) {
      clearInterval(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  };

  useEffect(() => {
    const container = logsContainerRef.current;
    if (container && logsEndRef.current) {
      const atBottom = container.scrollHeight - container.clientHeight <= container.scrollTop + 1;
      if (atBottom) {
        logsEndRef.current.scrollIntoView({ behavior: "smooth" });
      }
    }
  }, [logs]);

  const cancelTranslation = async () => {
    if (!jobIdRef.current) return;
    stopPolling();
    try {
      const config = backendConfigRef.current ?? await getBackendConfig();
      await fetch(`${backendBase(config)}/api/cancel/${jobIdRef.current}`, {
        method: "POST",
        headers: authHeaders(config),
      });
    } catch { /* ignore */ }
    setIsWorking(false);
    setStatus("已取消");
    setLogs(prev => [...prev, { text: "- 任务已取消", isError: false }]);
    jobIdRef.current = null;
  };

  const run = async () => {
    if (!file) return;
    stopPolling();
    setIsWorking(true);
    setProgress(0);
    setIsError(false);
    setStatus("正在启动后台服务...");
    setLogs([{ text: "- 正在启动后台服务，请稍候...", isError: false }]);

    let backendConfig: BackendConfig;
    try {
      backendConfig = await getBackendConfig();
    } catch (error) {
      const message = error instanceof Error ? error.message : "无法获取本地后台配置";
      setIsWorking(false);
      setIsError(true);
      setStatus("后台配置失败");
      setLogs(prev => [...prev, { text: `[错误] ${message}`, isError: true }]);
      return;
    }
    backendConfigRef.current = backendConfig;
    const activeBackendBase = backendBase(backendConfig);
    const activeAuthHeaders = authHeaders(backendConfig);

    let backendReady = false;
    for (let i = 0; i < BACKEND_READY_ATTEMPTS; i++) {
      try {
        const healthRes = await fetch(`${activeBackendBase}/api/health`, {
          method: "GET",
          headers: activeAuthHeaders,
        });
        if (healthRes.ok) { backendReady = true; break; }
      } catch { /* wait */ }
      await new Promise(r => setTimeout(r, 500));
    }

    if (!backendReady) {
      setIsWorking(false);
      setIsError(true);
      setLogs(prev => [...prev, { text: `[错误] 无法连接到本地后台服务，请确认 ${backendConfig.port} 端口没有被占用`, isError: true }]);
      setStatus("连接失败");
      return;
    }

    setStatus("正在准备翻译...");
    setLogs(prev => [...prev, { text: "- 后台服务就绪，开始提交翻译...", isError: false }]);

    const form = new FormData();
    form.append("file", file);
    form.append("api_key", apiKey);
    form.append("summary_model_name", sumModel);
    form.append("model_name", transModel);
    form.append("summary_prompt", sumPrompt);
    form.append("translation_prompt", transPrompt);
    form.append("save_path", savePath);
    form.append("glossary", glossary);
    form.append("max_output_tokens", String(maxOutputTokens));
    form.append("request_timeout", String(requestTimeout));
    form.append("save_merged_subtitles", String(saveMergedSubtitles));

    try {
      const res = await fetch(`${activeBackendBase}/api/translate`, {
        method: "POST",
        headers: activeAuthHeaders,
        body: form,
      });
      const data = await res.json();

      if (!data.job_id) {
        setIsWorking(false);
        setIsError(true);
        setStatus(data.error || "翻译请求失败");
        setLogs(prev => [...prev, { text: `[错误] ${data.error || "翻译请求失败"}`, isError: true }]);
        return;
      }

      jobIdRef.current = data.job_id;
      if (data.expected_report_output) {
        setLogs(prev => [...prev, { text: `- 报告将保存到: ${data.expected_report_output}`, isError: false }]);
      }

      pollingTimerRef.current = setInterval(async () => {
        try {
          const statusRes = await fetch(`${activeBackendBase}/api/status/${jobIdRef.current}`, {
            headers: activeAuthHeaders,
          });
          const statusData = await statusRes.json();

          if (statusData.logs && Array.isArray(statusData.logs)) {
            setLogs(statusData.logs);
          }

          switch (statusData.status) {
            case "error":
              stopPolling();
              setIsWorking(false);
              setIsError(true);
              setStatus("翻译失败，请查看下方日志");
              break;
            case "completed":
              stopPolling();
              setProgress(100);
              setIsWorking(false);
              setStatus("Agent 翻译完成！");
              break;
            case "completed_with_warnings":
              stopPolling();
              setProgress(100);
              setIsWorking(false);
              setStatus("Agent 翻译完成，但有字幕保留原文，请查看报告");
              setIsError(true);
              break;
            case "cancelled":
              stopPolling();
              setIsWorking(false);
              setStatus("已取消");
              break;
            default:
              setProgress(statusData.progress || 0);
              if (statusData.stage) {
                setStatus(`Agent ${statusData.stage}... ${statusData.progress || 0}%`);
              } else if ((statusData.progress || 0) >= 80) {
                setStatus(`Agent 正在复审... ${statusData.progress || 0}%`);
              } else {
                setStatus(`Agent 正在翻译... ${statusData.progress || 0}%`);
              }
          }
        } catch {
          stopPolling();
          setIsWorking(false);
          setIsError(true);
          setLogs(prev => [...prev, { text: "[错误] 与后台服务的连接断开", isError: true }]);
          setStatus("连接断开");
        }
      }, POLL_INTERVAL);
    } catch {
      setIsWorking(false);
      setIsError(true);
      setLogs(prev => [...prev, { text: "[错误] 无法连接到本地后台服务", isError: true }]);
      setStatus("连接失败");
    }
  };

  const normalizedProgress = clampProgress(progress);
  const activeStage = getActiveStage(normalizedProgress, isWorking, Boolean(file));
  const statusTone = isError ? "error" : isWorking ? "active" : normalizedProgress === 100 ? "done" : file ? "ready" : "idle";
  const statusLabel = isError ? "需要检查" : isWorking ? "运行中" : normalizedProgress === 100 ? "已完成" : file ? "已就绪" : "等待输入";
  const outputTarget = savePath.trim() || "桌面";
  const modelRoute = `${sumModel} → ${transModel}`;

  return (
    <div className="view-content console-view">
      <section className="workflow-overview">
        <div className="upload-console">
          <div className="panel-caption">
            <span>Source</span>
            <span className={`caption-state ${file ? "ready" : ""}`}>{file ? "SRT READY" : "WAITING"}</span>
          </div>
          <div className="ti8-upload">
            <input type="file" accept=".srt" onChange={(e) => {
              if (e.target.files?.[0]) {
                setFile(e.target.files[0]);
                setLogs([{ text: `- 已选择文件: ${e.target.files[0].name}`, isError: false }]);
                setStatus("文件已就绪");
                setProgress(0); setIsWorking(false); setIsError(false);
              }
            }} id="ti-up" />
            <label htmlFor="ti-up" className={`ti8-btn-upload ${file ? "has-file" : ""}`}>
              <span className="upload-badge">SRT</span>
              <span className="upload-copy">
                <span className="label-main">{file ? file.name : "选择字幕文件"}</span>
                <span className="label-sub">
                  {file ? `${formatFileSize(file.size)} · 点击更换文件` : "导入 .srt，保留时间轴与字幕序号"}
                </span>
              </span>
            </label>
          </div>
        </div>

        <div className="action-stack">
          <button className={`ti8-execute ${file ? "ready" : ""}`} onClick={run} disabled={!file || isWorking}>
            <span>{isWorking ? "执行中" : "开始翻译"}</span>
            <small>Agent Pipeline</small>
          </button>

          {isWorking && (
            <button className="ti8-cancel-btn" onClick={cancelTranslation}>
              取消任务
            </button>
          )}
        </div>
      </section>

      <section className="workflow-panel">
        <div className="section-bar">
          <div>
            <span className="section-kicker">Workflow</span>
            <h2>字幕处理流水线</h2>
          </div>
          <span className={`status-pill ${statusTone}`}>{statusLabel}</span>
        </div>

        <div className="progress-row">
          <div className="progress-container" aria-label="翻译进度">
            <div className="progress-bar" style={{ width: `${normalizedProgress}%` }} />
          </div>
          <span className="progress-value">{normalizedProgress}%</span>
        </div>

        <div className="stage-grid">
          {WORKFLOW_STAGES.map((stage, index) => (
            <div key={stage} className={`stage-node ${stageClass(index, activeStage, normalizedProgress, isWorking, isError)}`}>
              <span className="stage-index">{String(index + 1).padStart(2, "0")}</span>
              <span className="stage-label">{stage}</span>
            </div>
          ))}
        </div>

        <div className={`ti8-status ${isError ? "is-error" : ""}`}>{status}</div>
      </section>

      <section className="run-details">
        <div className="detail-row">
          <span>输入</span>
          <strong>{file ? file.name : "未选择"}</strong>
        </div>
        <div className="detail-row">
          <span>输出目录</span>
          <strong>{outputTarget}</strong>
        </div>
        <div className="detail-row">
          <span>模型链路</span>
          <strong>{modelRoute}</strong>
        </div>
        <div className="detail-row">
          <span>报告</span>
          <strong>质量审计 + 保留原文提示</strong>
        </div>
      </section>

      <section className="log-panel">
        <div className="section-bar compact">
          <div>
            <span className="section-kicker">Console</span>
            <h2>运行日志</h2>
          </div>
        </div>
        <div className="terminal-logs-container" ref={logsContainerRef}>
          {(logs || []).map((log, index) => (
            <div key={index} className={`log-line ${log.isError ? "error" : ""}`}>
              {log.text}
            </div>
          ))}
          <div ref={logsEndRef} />
        </div>
      </section>
    </div>
  );
}
