import { useState, useEffect } from "react";
import "./App.css";
import { useSettings } from "./hooks/useSettings";
import SettingsPanel from "./components/SettingsPanel";
import TranslationView from "./components/TranslationView";
import PromptModal, { type ModalType } from "./components/PromptModal";

type LogEntry = { text: string; isError: boolean };

function App() {
  const settings = useSettings();
  const [file, setFile] = useState<File | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [activeModal, setActiveModal] = useState<ModalType>(null);

  const [isWorking, setIsWorking] = useState(false);
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState("请选择字幕文件");
  const [isError, setIsError] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([{ text: "- 等待操作...", isError: false }]);

  useEffect(() => {
    document.title = "NBL Subtitle Task Force";
  }, []);

  return (
    <div className="ti8-container">
      <div className="terminal-card">
        <div className="card-header">
          <div className="app-brand">
            <span className="brand-mark">ST</span>
            <div className="brand-copy">
              <span className="brand-title">NBL Subtitle Task Force</span>
              <span className="brand-subtitle">Subtitle Task Force · DeepSeek · SRT</span>
            </div>
          </div>
          <div className="header-actions">
            <span className={`run-indicator ${isWorking ? "active" : isError ? "error" : ""}`}>
              {isWorking ? "RUNNING" : isError ? "REVIEW" : "READY"}
            </span>
            <button className="settings-toggle" onClick={() => setShowSettings(!showSettings)}>
              {showSettings ? "工作台" : "设置"}
            </button>
          </div>
        </div>

        {!showSettings ? (
          <TranslationView
            file={file} setFile={setFile}
            isWorking={isWorking} setIsWorking={setIsWorking}
            progress={progress} setProgress={setProgress}
            status={status} setStatus={setStatus}
            isError={isError} setIsError={setIsError}
            logs={logs} setLogs={setLogs}
            apiKey={settings.apiKey}
            sumModel={settings.sumModel} transModel={settings.transModel}
            sumPrompt={settings.sumPrompt} transPrompt={settings.transPrompt}
            savePath={settings.savePath} glossary={settings.glossary}
            maxOutputTokens={settings.maxOutputTokens}
            requestTimeout={settings.requestTimeout}
            saveMergedSubtitles={settings.saveMergedSubtitles}
          />
        ) : (
          <SettingsPanel
            apiKey={settings.apiKey} setApiKey={settings.setApiKey}
            sumModel={settings.sumModel} setSumModel={settings.setSumModel}
            transModel={settings.transModel} setTransModel={settings.setTransModel}
            savePath={settings.savePath} setSavePath={settings.setSavePath}
            maxOutputTokens={settings.maxOutputTokens} setMaxOutputTokens={settings.setMaxOutputTokens}
            requestTimeout={settings.requestTimeout} setRequestTimeout={settings.setRequestTimeout}
            saveMergedSubtitles={settings.saveMergedSubtitles} setSaveMergedSubtitles={settings.setSaveMergedSubtitles}
            setActiveModal={setActiveModal}
            resetToDefaults={settings.resetToDefaults}
          />
        )}
      </div>

      <PromptModal
        activeModal={activeModal}
        setActiveModal={setActiveModal}
        sumPrompt={settings.sumPrompt}
        setSumPrompt={settings.setSumPrompt}
        transPrompt={settings.transPrompt}
        setTransPrompt={settings.setTransPrompt}
        glossary={settings.glossary}
        setGlossary={settings.setGlossary}
      />
    </div>
  );
}

export default App;
