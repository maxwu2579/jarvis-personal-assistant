"""生成 JARVIS 双语学习 DOCX 文档（可重复运行）。

用法：
    python generate_learning_docs.py            # 生成全部已实现阶段的文档
    python generate_learning_docs.py phase0     # 只生成 Phase 0
    python generate_learning_docs.py phase1     # 只生成 Phase 1

输出：docs/learning/ 下按阶段命名的 .docx 文件。
排版规范见 docx_builder.py 模块注释。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docx_builder import BilingualDoc  # noqa: E402

import content_phase0  # noqa: E402
import content_phase1  # noqa: E402
import content_phase2  # noqa: E402
import content_phase3  # noqa: E402
import content_phase4  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "docs" / "learning"

PHASES = {
    "phase0": ("阶段0_Phase0_后端基础与任务状态机.docx", content_phase0),
    "phase1": ("阶段1_Phase1_LLM_Gateway与对话系统.docx", content_phase1),
    "phase2": ("阶段2_Phase2_结构化输出与工具建议.docx", content_phase2),
    "phase3": ("阶段3_Phase3_Reminder提醒调度.docx", content_phase3),
    "phase4": ("阶段4_Phase4_Document_Analysis与RAG.docx", content_phase4),
}


def generate(phase_key: str) -> Path:
    if phase_key not in PHASES:
        raise SystemExit(f"未知阶段 {phase_key!r}，可选：{', '.join(PHASES)}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename, content = PHASES[phase_key]
    doc = BilingualDoc(content.META)
    doc.add_toc()
    content.build(doc)
    output_path = OUTPUT_DIR / filename
    doc.save(str(output_path))
    print(f"[OK] {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
    return output_path


def main() -> None:
    targets = sys.argv[1:] or list(PHASES)
    for target in targets:
        generate(target)


if __name__ == "__main__":
    main()
