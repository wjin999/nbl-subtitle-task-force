export type ModalType = "sum" | "trans" | "glossary" | null;

interface Props {
  activeModal: ModalType;
  setActiveModal: (m: ModalType) => void;
  sumPrompt: string;
  setSumPrompt: (v: string) => void;
  transPrompt: string;
  setTransPrompt: (v: string) => void;
  glossary: string;
  setGlossary: (v: string) => void;
}

function getModalTitle(type: ModalType): string {
  switch (type) {
    case "sum": return "Agent 分析提示词";
    case "trans": return "Agent 翻译提示词";
    case "glossary": return "自定义术语表";
    default: return "";
  }
}

function getModalValue(props: Props): string {
  const { activeModal, sumPrompt, transPrompt, glossary } = props;
  if (activeModal === "sum") return sumPrompt;
  if (activeModal === "trans") return transPrompt;
  if (activeModal === "glossary") return glossary;
  return "";
}

export default function PromptModal(props: Props) {
  const { activeModal, setActiveModal, setSumPrompt, setTransPrompt, setGlossary } = props;
  if (!activeModal) return null;

  const setValue = (val: string) => {
    if (activeModal === "sum") setSumPrompt(val);
    else if (activeModal === "trans") setTransPrompt(val);
    else if (activeModal === "glossary") setGlossary(val);
  };

  return (
    <div className="ti8-overlay">
      <div className="ti8-modal">
        <div className="modal-top">
          <span>{getModalTitle(activeModal)}</span>
          <button className="modal-save" onClick={() => setActiveModal(null)}>
            确认保存
          </button>
        </div>

        {activeModal === "glossary" && (
          <div className="glossary-hint">
            * 此内容直接提供给大模型作为翻译参考。<br />
            * 建议格式：原文: 译文（冒号中英皆可），每行一个。<br />
            * 示例：<br />
            &nbsp;&nbsp;Cyberpunk: 赛博朋克<br />
            &nbsp;&nbsp;Night City: 夜之城
          </div>
        )}

        <textarea
          value={getModalValue(props)}
          onChange={(e) => setValue(e.target.value)}
          className={activeModal === "glossary" ? "is-glossary" : ""}
          spellCheck={false}
        />
      </div>
    </div>
  );
}
