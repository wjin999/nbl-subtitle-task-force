import { useState, useEffect, useRef } from "react";

export const DEF_URL = "https://api.deepseek.com";
export const DEF_SUM_MODEL = "deepseek-v4-pro";
export const DEF_TRANS_MODEL = "deepseek-v4-pro";
const PROMPT_PRESET_VERSION = "nbl-agent-v3";

export const _SUM_DEF =
  `你是 NBL Subtitle Task Force 的前置分析员。字幕来自网络视频，目标译文面向普通中文观众，必须通俗易懂、自然顺口。

请阅读字幕样本，为后续翻译 Agent 生成一份稳定的中文翻译策略。
必须覆盖：内容主题、人物关系、整体语气、术语称呼、文化梗风险、通俗表达策略、字幕长度与标点策略、需要后续复审重点盯住的问题。

## NBL Subtitle Task Force 工作协议：
1. 先读上下文，再动手：综合当前窗口、前后文、术语表、翻译记忆和全局 AgentPlan 判断含义。
2. 分阶段闭环：分析主题与风险 -> 归并全局计划 -> 生成草稿 -> 复审修正 -> 一致性审计 -> 报告风险。
3. 输出契约优先：要求 JSON 的阶段只输出合法 JSON；条目数量、id、时间轴和字幕顺序必须保持稳定。
4. 证据优先：不要凭空补剧情、人物关系或专有名词。
5. 用户规则优先：自定义术语表和用户提示词优先于模型推断。
6. 质量自检：检查漏译、错译、翻译腔、术语漂移、人物语气漂移、字幕过长和标点违规。

输出中文，控制在 500 字以内。`;

export const _TRANS_DEF = `你是 NBL Subtitle Task Force 翻译流水线中的翻译执行者，负责将英语视频字幕翻译成简体中文。

## 字幕质量目标：
面向普通中文观众，译文必须通俗易懂、自然顺口、信息清楚。优先让观众立刻理解视频内容，而不是逐词对应英文。

## 核心要求：
1. 输出合法 JSON 格式：{"translations": [{"id": 0, "text": "翻译"}, ...]}
2. 输出的条目数量必须与输入完全一致
3. 每条译文必须简洁，适合屏幕阅读
4. 遵循 AgentPlan、术语表、用户提示词和翻译记忆
5. 输出前自检漏译、错译、过长、翻译腔、术语不一致

## 视频字幕翻译风格：
1. 使用自然、口语化的中文，像中文创作者会说的话
2. 保留说话者的语气和情感（愤怒、低语、讽刺、兴奋、吐槽等）
3. 保持角色/主播语气在全篇字幕中一致
4. 技术、生活、网络和频道相关表达优先使用大众熟悉的说法
5. 遇到习语、双关语、俚语或文化特定内容时，采用意译而非直译
6. 不要过度书面、文艺、影视腔或机器翻译腔

## NBL Subtitle Task Force 工作协议：
1. 先读上下文，再动手：综合当前窗口、前后文、术语表、翻译记忆和全局 AgentPlan 判断含义。
2. 分阶段闭环：分析主题与风险 -> 归并全局计划 -> 生成草稿 -> 复审修正 -> 一致性审计 -> 报告风险。
3. 输出契约优先：要求 JSON 的阶段只输出合法 JSON；条目数量、id、时间轴和字幕顺序必须保持稳定。
4. 证据优先：不要凭空补剧情、人物关系或专有名词。
5. 用户规则优先：自定义术语表和用户提示词优先于模型推断。
6. 质量自检：检查漏译、错译、翻译腔、术语漂移、人物语气漂移、字幕过长和标点违规。

## 字幕标点规范（必须严格遵守）：
1. 句末不加句号（。）
2. 必须保留问号（？）和叹号（！）
3. 句中停顿用空格代替逗号（，）

## JSON 格式示例：
{"translations": [{"id": 0, "text": "你好"}, {"id": 1, "text": "世界"}]}`;

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function loadPromptPresetVersion() {
  return load("promptPresetVersion", "");
}

function isLegacyAnalysisPrompt(value: string) {
  return (
    Boolean(value)
    && !value.includes("NBL Subtitle Task Force")
    && value.includes("文化梗风险")
    && value.includes("前置分析员")
  );
}

function isLegacyTranslationPrompt(value: string) {
  return (
    Boolean(value)
    && !value.includes("NBL Subtitle Task Force")
    && value.includes("JSON 格式示例")
    && value.includes("字幕标点规范")
    && value.includes("翻译流水线")
  );
}

export function useSettings() {
  const [apiKey, setApiKey] = useState("");
  const [sumModel, setSumModel] = useState(() => load("sumModel", DEF_SUM_MODEL));
  const [transModel, setTransModel] = useState(() => load("transModel", DEF_TRANS_MODEL));
  const [sumPrompt, setSumPrompt] = useState(() => load("sumPrompt", _SUM_DEF));
  const [transPrompt, setTransPrompt] = useState(() => load("transPrompt", _TRANS_DEF));
  const [savePath, setSavePath] = useState(() => load("savePath", ""));
  const [glossary, setGlossary] = useState(() => load("glossary", ""));
  const [maxOutputTokens, setMaxOutputTokens] = useState(() => load("maxOutputTokens", 4096));
  const [requestTimeout, setRequestTimeout] = useState(() => load("requestTimeout", 60));
  const [saveMergedSubtitles, setSaveMergedSubtitles] = useState(() => load("saveMergedSubtitles", false));

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    localStorage.removeItem("apiKey");
    if (loadPromptPresetVersion() === PROMPT_PRESET_VERSION) return;

    const storedSumPrompt = load("sumPrompt", "");
    const storedTransPrompt = load("transPrompt", "");
    if (!storedSumPrompt || isLegacyAnalysisPrompt(storedSumPrompt)) {
      setSumPrompt(_SUM_DEF);
    }
    if (!storedTransPrompt || isLegacyTranslationPrompt(storedTransPrompt)) {
      setTransPrompt(_TRANS_DEF);
    }
    localStorage.setItem("promptPresetVersion", JSON.stringify(PROMPT_PRESET_VERSION));
  }, []);

  // Debounced localStorage sync: 500ms after last change
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      localStorage.removeItem("apiKey");
      localStorage.setItem("sumModel", JSON.stringify(sumModel));
      localStorage.setItem("transModel", JSON.stringify(transModel));
      localStorage.setItem("sumPrompt", JSON.stringify(sumPrompt));
      localStorage.setItem("transPrompt", JSON.stringify(transPrompt));
      localStorage.setItem("savePath", JSON.stringify(savePath));
      localStorage.setItem("glossary", JSON.stringify(glossary));
      localStorage.setItem("maxOutputTokens", JSON.stringify(maxOutputTokens));
      localStorage.setItem("requestTimeout", JSON.stringify(requestTimeout));
      localStorage.setItem("saveMergedSubtitles", JSON.stringify(saveMergedSubtitles));
      localStorage.setItem("promptPresetVersion", JSON.stringify(PROMPT_PRESET_VERSION));
    }, 500);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [apiKey, sumModel, transModel, sumPrompt, transPrompt, savePath, glossary, maxOutputTokens, requestTimeout, saveMergedSubtitles]);

  const resetToDefaults = () => {
    setApiKey("");
    setSumModel(DEF_SUM_MODEL);
    setTransModel(DEF_TRANS_MODEL);
    setSumPrompt(_SUM_DEF);
    setTransPrompt(_TRANS_DEF);
    setSavePath("");
    setGlossary("");
    setMaxOutputTokens(4096);
    setRequestTimeout(60);
    setSaveMergedSubtitles(false);
    localStorage.removeItem("apiKey");
    localStorage.setItem("promptPresetVersion", JSON.stringify(PROMPT_PRESET_VERSION));
  };

  return {
    apiKey, setApiKey,
    sumModel, setSumModel,
    transModel, setTransModel,
    sumPrompt, setSumPrompt,
    transPrompt, setTransPrompt,
    savePath, setSavePath,
    glossary, setGlossary,
    maxOutputTokens, setMaxOutputTokens,
    requestTimeout, setRequestTimeout,
    saveMergedSubtitles, setSaveMergedSubtitles,
    resetToDefaults,
  };
}
