"""
ระบบ Local RAG สำหรับงานซ่อมบำรุงเครื่องจักร
- ใช้ Ollama เป็น LLM/Embedding (รันบนเครื่อง)
- ใช้ ChromaDB เป็น Vector Database (persist ลงดิสก์)
- ใช้ LangChain สำหรับ pipeline Retrieval + Generation

วิธีใช้งาน (สรุป):
1) ติดตั้ง dependencies จาก requirements.txt
2) เปิด Ollama และ pull โมเดลที่ต้องใช้
3) รันไฟล์นี้ แล้วพิมพ์คำถามใน Terminal
4) พิมพ์ exit เพื่อจบโปรแกรม
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama, OllamaEmbeddings


# =========================
# ส่วนตั้งค่า (Config)
# =========================
BASE_DIR = Path(__file__).resolve().parent
MANUALS_DIR = BASE_DIR / "manuals"
LEGACY_DATA_FILE = BASE_DIR / "maintenance_log.txt"
MANUAL_FILE_EXTENSIONS = {".txt", ".docx"}
CHROMA_DIR = BASE_DIR / "chroma_db"
INDEX_MANIFEST_FILE = CHROMA_DIR / "index_manifest.json"
LOGS_DIR = BASE_DIR / "logs"
QUESTION_LOG_FILENAME = "question_logs.csv"
COLLECTION_NAME = "machine_maintenance_knowledge"
INDEX_SCHEMA_VERSION = "v2_strict_record_split"
NOT_FOUND_NOTICE = "ไม่พบข้อมูลที่ระบุในคู่มือเครื่องจักร กรุณาตรวจสอบรหัสอาการหรือชื่อเครื่องใหม่อีกครั้ง"
REQUIRE_MACHINE_ALARM_NOTICE = "กรุณาระบุชื่อเครื่องและ Alarm ก่อนถาม เช่น: MACHINE: BROTHER TC-S2A NC Alarm 5566"


class RuntimeInitializationError(RuntimeError):
    """Raised when the RAG runtime cannot be initialized."""

# สามารถเปลี่ยนโมเดลผ่าน Environment Variable ได้
# ตัวอย่าง:
#   set OLLAMA_LLM_MODEL=llama3.1:8b
#   set OLLAMA_EMBED_MODEL=nomic-embed-text
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "bge-m3")
# OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "llama3.1:8b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# บังคับ re-index ทุกครั้งได้ผ่าน env (ปกติไม่ต้องเปิด)
# set FORCE_REBUILD_INDEX=true
FORCE_REBUILD_INDEX = os.getenv("FORCE_REBUILD_INDEX", "false").lower() == "true"


# =========================
# Utility Functions
# =========================
def discover_manual_files() -> list[Path]:
    """
    ค้นหาไฟล์คู่มือหลายไฟล์ในโฟลเดอร์เดียว
    พฤติกรรมการค้นหา:
    1) อ่านไฟล์ .txt ทุกไฟล์จากโฟลเดอร์ manuals (ถ้ามี)
    2) รวม maintenance_log.txt เดิมเข้าไปด้วย (ถ้ามี)
    3) คืนค่าไฟล์ที่ไม่ซ้ำกันและเรียงชื่อ
    """
    manual_files: list[Path] = []

    if MANUALS_DIR.exists() and MANUALS_DIR.is_dir():
        for path in sorted(MANUALS_DIR.iterdir()):
            if (
                path.is_file()
                and path.suffix.lower() in MANUAL_FILE_EXTENSIONS
                and not path.name.startswith("~$")
            ):
                manual_files.append(path)

    if LEGACY_DATA_FILE.exists() and LEGACY_DATA_FILE.is_file():
        manual_files.append(LEGACY_DATA_FILE)

    if manual_files:
        # กันไฟล์ซ้ำกรณีชื่อเดียวกันหรือ path ซ้ำ
        uniq = sorted({file_path.resolve() for file_path in manual_files})
        return uniq

    raise FileNotFoundError(
        f"ไม่พบไฟล์คู่มือ: กรุณาสร้างโฟลเดอร์ {MANUALS_DIR.name} และวางไฟล์ .txt/.docx หรือสร้าง {LEGACY_DATA_FILE.name}"
    )


def load_manual_text(file_path: Path) -> str:
    """อ่านไฟล์คู่มือ/ประวัติซ่อมบำรุง รองรับ .txt และ .docx"""
    suffix = file_path.suffix.lower()

    if suffix == ".txt":
        return file_path.read_text(encoding="utf-8")

    if suffix == ".docx":
        try:
            docx_module = importlib.import_module("docx")
            DocxDocument = getattr(docx_module, "Document")
        except ImportError as exc:
            raise RuntimeError(
                "ยังไม่ได้ติดตั้ง python-docx กรุณารัน: pip install python-docx"
            ) from exc

        doc = DocxDocument(str(file_path))
        lines: list[str] = []

        # อ่านย่อหน้าในเอกสารตามลำดับ
        for paragraph in doc.paragraphs:
            text = paragraph.text.rstrip()
            if text:
                lines.append(text)

        # รองรับกรณีข้อมูลถูกเก็บในตาราง
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    if cell_text:
                        lines.append(cell_text)

        return "\n".join(lines)

    raise ValueError(f"ยังไม่รองรับไฟล์นามสกุล: {file_path.suffix}")


def split_by_machine_record(raw_text: str) -> list[str]:
    """
    แบ่งข้อมูลโดยยึด separator = "=== END===" ตามที่กำหนด
    เพื่อให้ 1 เคสอาการ/สาเหตุ/วิธีแก้ = 1 chunk ชัดเจน

    หมายเหตุ:
    - ตั้ง chunk_size ใหญ่มาก เพื่อไม่ให้เกิดการซอยย่อยเพิ่ม
    - การแยกหลักจึงพึ่งพา separator เป็นหลัก
    """
    separator_pattern = re.compile(r"===\s*end\s*===", re.IGNORECASE)
    raw_parts = separator_pattern.split(raw_text)

    strict_chunks: list[str] = []
    for part in raw_parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        strict_chunks.append(f"{cleaned}\n=== END ===")

    return strict_chunks


def build_documents(chunks: list[str], source_file: Path) -> list[Document]:
    """แปลงข้อความแต่ละ chunk เป็น LangChain Document พร้อม metadata"""
    docs: list[Document] = []
    for idx, text in enumerate(chunks, start=1):
        docs.append(
            Document(
                page_content=text,
                metadata={
                    "source": str(source_file.name),
                    "chunk_id": idx,
                },
            )
        )
    return docs


def compute_files_signature(files: list[Path]) -> str:
    """
    สร้างลายนิ้วมือรวมของไฟล์คู่มือทั้งหมด
    ใช้สำหรับตรวจว่าไฟล์มีการเปลี่ยนแปลงหรือไม่
    """
    h = hashlib.sha256()
    # รวมเวอร์ชันโครงสร้างดัชนี เพื่อบังคับ rebuild เมื่อ logic split เปลี่ยน
    h.update(INDEX_SCHEMA_VERSION.encode("utf-8"))
    for file_path in sorted(files):
        content_bytes = file_path.read_bytes()
        h.update(file_path.name.encode("utf-8"))
        h.update(str(len(content_bytes)).encode("utf-8"))
        h.update(content_bytes)
    return h.hexdigest()


def load_manifest_signature() -> Optional[str]:
    """อ่าน signature เดิมจากไฟล์ manifest ถ้ามี"""
    if not INDEX_MANIFEST_FILE.exists():
        return None

    try:
        payload = json.loads(INDEX_MANIFEST_FILE.read_text(encoding="utf-8"))
        value = payload.get("files_signature")
        return value if isinstance(value, str) else None
    except Exception:
        return None


def save_manifest_signature(files_signature: str, files: list[Path], docs_count: int) -> None:
    """บันทึก signature ล่าสุดหลังสร้าง index สำเร็จ"""
    manifest = {
        "files_signature": files_signature,
        "files": [str(file_path.name) for file_path in files],
        "documents_count": docs_count,
    }
    INDEX_MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _describe_vectorstore_error(exc: Exception) -> str:
    """แปลงข้อผิดพลาดจาก Ollama/Chroma ให้เป็นข้อความที่ใช้งานได้จริง"""
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()

    if any(keyword in lowered for keyword in ("tokenize", "connectex", "actively refused", "connection refused")):
        return (
            "ไม่สามารถเชื่อมต่อ Ollama embedding service ได้ "
            f"(base_url={OLLAMA_BASE_URL}, model={OLLAMA_EMBED_MODEL}) "
            "กรุณาเปิด Ollama และตรวจสอบว่าโมเดล embedding พร้อมใช้งาน"
        )

    if "model" in lowered and "not found" in lowered:
        return (
            f"ไม่พบ embedding model '{OLLAMA_EMBED_MODEL}' ใน Ollama "
            "กรุณา pull โมเดลก่อนใช้งาน"
        )

    return f"ไม่สามารถเตรียมฐานข้อมูลเวกเตอร์ได้: {message}"


def prepare_vectorstore(documents: list[Document], files_signature: str) -> Chroma:
    """
    สร้างหรือโหลด ChromaDB
    - re-index เมื่อไฟล์คู่มือเปลี่ยนเท่านั้น
    - หรือบังคับ re-index ด้วย FORCE_REBUILD_INDEX=true
    - persist ลงโฟลเดอร์ ./chroma_db
    """
    embeddings = OllamaEmbeddings(
        model=OLLAMA_EMBED_MODEL,
        base_url=OLLAMA_BASE_URL,
    )

    previous_signature = load_manifest_signature()
    has_index = CHROMA_DIR.exists()
    index_changed = previous_signature != files_signature

    should_rebuild = FORCE_REBUILD_INDEX or (not has_index) or index_changed

    if should_rebuild:
        rebuild_dir = CHROMA_DIR if not has_index else CHROMA_DIR.parent / f"chroma_db_rebuild_{os.getpid()}"
        try:
            if rebuild_dir.exists() and rebuild_dir != CHROMA_DIR:
                shutil.rmtree(rebuild_dir)
            vectorstore = Chroma.from_documents(
                documents=documents,
                embedding=embeddings,
                persist_directory=str(rebuild_dir),
                collection_name=COLLECTION_NAME,
            )

            if rebuild_dir != CHROMA_DIR:
                backup_dir = CHROMA_DIR.parent / f"chroma_db_backup_{os.getpid()}"
                try:
                    if backup_dir.exists():
                        shutil.rmtree(backup_dir)
                    if CHROMA_DIR.exists():
                        CHROMA_DIR.replace(backup_dir)
                    rebuild_dir.replace(CHROMA_DIR)
                    if backup_dir.exists():
                        shutil.rmtree(backup_dir)
                except OSError:
                    # ถ้าสลับโฟลเดอร์ไม่ได้บน Windows ให้ใช้ index ที่สร้างใหม่ใน process นี้ต่อไป
                    return vectorstore

            return vectorstore
        except OSError:
            # Windows ล็อก chroma.sqlite3 ได้ ถ้าลบ/สร้างใหม่ไม่ได้ให้ใช้ index เดิมแทน
            should_rebuild = False
        except Exception as exc:
            if has_index:
                should_rebuild = False
            else:
                raise RuntimeInitializationError(_describe_vectorstore_error(exc)) from exc

    # กรณีไฟล์ไม่เปลี่ยน ให้โหลด index เดิม
    try:
        return Chroma(
            persist_directory=str(CHROMA_DIR),
            embedding_function=embeddings,
            collection_name=COLLECTION_NAME,
        )
    except Exception as exc:
        # ถ้า index เดิมก็เปิดไม่ได้ ให้สร้างใหม่แบบไม่ลบของเก่าด้วยโฟลเดอร์สำรอง
        fallback_dir = CHROMA_DIR.parent / f"chroma_db_fallback_{os.getpid()}"
        try:
            if fallback_dir.exists():
                shutil.rmtree(fallback_dir)
            return Chroma.from_documents(
                documents=documents,
                embedding=embeddings,
                persist_directory=str(fallback_dir),
                collection_name=COLLECTION_NAME,
            )
        except Exception as fallback_exc:
            root_error = fallback_exc if isinstance(fallback_exc, RuntimeInitializationError) else exc
            raise RuntimeInitializationError(_describe_vectorstore_error(root_error)) from fallback_exc


def warmup_runtime(runtime: dict[str, object]) -> None:
    """อุ่นโมเดลและ pipeline แบบเบา ๆ เพื่อให้คำถามแรกตอบไวขึ้น"""
    llm = runtime.get("llm")
    prompt = runtime.get("prompt")

    if llm is None or prompt is None:
        return

    try:
        messages = prompt.format_messages(
            context="warmup",
            question="warmup",
        )
        llm.invoke(messages)
    except Exception:
        # warmup เป็นงานเสริม ถ้าล้มไม่ควรทำให้ระบบหลักล้มตาม
        pass


def parse_record_from_text(text: str) -> dict[str, str]:
    """
    พยายามดึงข้อมูลโครงสร้างจาก chunk สำหรับ fallback
    ใช้เมื่อโมเดลตอบไม่ตรง format ที่บังคับ
    """
    machine_match = re.search(r"===\s*MACHINE:\s*(.*?)\s*===", text, re.IGNORECASE)
    symptom_match = re.search(
        r"(?:^|\n)\s*(?:\[อาการ\]|อาการ)\s*[:：]?\s*(.*)",
        text,
        re.MULTILINE,
    )
    cause_match = re.search(
        r"(?:^|\n)\s*(?:\[สาเหตุ\]|สาเหตุ)\s*[:：]?\s*(.*?)(?:\n\s*(?:\[(?:การแก้ไข|วิธีการแก้ไข)\]|(?:การแก้ไข|วิธีการแก้ไข))\s*[:：]?|\n\s*===\s*END\s*===|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    fix_match = re.search(
        r"(?:^|\n)\s*(?:\[(?:การแก้ไข|วิธีการแก้ไข)\]|(?:การแก้ไข|วิธีการแก้ไข))\s*[:：]?\s*(.*?)(?:\n\s*(?:\[[^\]]+\]|(?:อาการ|สาเหตุ|การแก้ไข|วิธีการแก้ไข))\s*[:：]?|\n\s*===\s*END\s*===|\n\s*===\s*MACHINE\s*:|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    return {
        "machine": machine_match.group(1).strip() if machine_match else "ไม่พบข้อมูล",
        "symptom": symptom_match.group(1).strip() if symptom_match else "ไม่พบข้อมูล",
        "cause": cause_match.group(1).strip() if cause_match else "ไม่พบข้อมูล",
        "fix": fix_match.group(1).strip() if fix_match else "ไม่พบข้อมูล",
    }


def normalize_output_or_fallback(raw_answer: str, top_doc_text: Optional[str]) -> str:
    """
    บังคับรูปแบบ output ให้เป็น 4 บรรทัดตาม requirement
    ถ้า LLM ตอบไม่ครบ จะ fallback จากข้อมูลที่ parse ได้จากเอกสาร top-1
    """
    pattern = re.compile(
        r"MACHINE\s*:\s*(.*?)\nอาการ\s*:\s*(.*?)\nสาเหตุ\s*:\s*(.*?)\nการแก้ไข\s*:\s*(.*)",
        re.DOTALL,
    )

    m = pattern.search(raw_answer.strip())
    if m:
        machine = m.group(1).strip() or "ไม่พบข้อมูล"
        symptom = m.group(2).strip() or "ไม่พบข้อมูล"
        cause = m.group(3).strip() or "ไม่พบข้อมูล"
        fix = m.group(4).strip() or "ไม่พบข้อมูล"
        return (
            f"MACHINE: {machine}\n"
            f"อาการ: {symptom}\n"
            f"สาเหตุ: {cause}\n"
            f"การแก้ไข: {fix}"
        )

    # fallback เมื่อ output หลุด format
    if top_doc_text:
        parsed = parse_record_from_text(top_doc_text)
        return (
            f"MACHINE: {parsed['machine']}\n"
            f"อาการ: {parsed['symptom']}\n"
            f"สาเหตุ: {parsed['cause']}\n"
            f"การแก้ไข: {parsed['fix']}"
        )

    return (
        "MACHINE: ไม่พบข้อมูล\n"
        "อาการ: ไม่พบข้อมูล\n"
        "สาเหตุ: ไม่พบข้อมูล\n"
        "การแก้ไข: ไม่พบข้อมูล"
    )


def format_answer_from_record(record: dict[str, str]) -> str:
    """จัดรูปแบบคำตอบมาตรฐานจากข้อมูล record ที่ parse แล้ว"""
    def prettify_multiline_field(text: str) -> str:
        """จัดรูปแบบ list หลายบรรทัดให้อ่านง่าย โดยไม่แก้เนื้อหาหลัก"""
        raw = (text or "").replace("\r\n", "\n").strip()
        if not raw:
            return "ไม่พบข้อมูล"

        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if not lines:
            return "ไม่พบข้อมูล"

        numeric_marker_pattern = re.compile(r"^(\d+)\s*[.)]\s*(.*)$")
        alpha_marker_pattern = re.compile(r"^\(?([A-Za-z])\)?\s*[.)]?\s+(.*)$")

        numeric_count = 0
        alpha_count = 0
        for line in lines:
            if numeric_marker_pattern.match(line):
                numeric_count += 1
            elif re.match(r"^\([A-Za-z]\)\s*", line) or re.match(r"^[A-Za-z][.)]\s*", line):
                alpha_count += 1

        has_numeric_list = numeric_count >= 1
        has_alpha_list = alpha_count >= 1

        formatted_lines: list[str] = []
        for line in lines:
            numeric_match = numeric_marker_pattern.match(line)
            if numeric_match:
                index = int(numeric_match.group(1))
                content = numeric_match.group(2).strip()
                formatted_lines.append(f"{index}. {content}".rstrip())
                continue

            alpha_match = alpha_marker_pattern.match(line)
            if alpha_match and (line.startswith("(") or re.match(r"^[A-Za-z][.)]\s*", line)):
                marker = alpha_match.group(1).lower()
                content = alpha_match.group(2).strip()
                formatted_lines.append(f"({marker}) {content}".rstrip())
                continue

            if formatted_lines:
                previous_line = formatted_lines[-1]
                if has_numeric_list and re.match(r"^\d+\.\s", previous_line):
                    formatted_lines.append(f"   {line}")
                    continue
                if has_alpha_list and re.match(r"^\([a-z]\)\s", previous_line):
                    formatted_lines.append(f"   {line}")
                    continue

            formatted_lines.append(line)

        return "\n".join(formatted_lines).strip()

    machine = (record.get("machine") or "").strip() or "ไม่พบข้อมูล"
    symptom = (record.get("symptom") or "").strip() or "ไม่พบข้อมูล"
    cause = prettify_multiline_field(record.get("cause", ""))
    fix = prettify_multiline_field(record.get("fix", ""))

    return (
        f"MACHINE: {machine}\n"
        f"อาการ: {symptom}\n"
        f"สาเหตุ: {cause}\n"
        f"การแก้ไข: {fix}"
    )


def normalize_for_match(text: str) -> str:
    """normalize ข้อความสำหรับเทียบแบบไม่สนตัวพิมพ์และช่องว่าง"""
    return re.sub(r"\s+", " ", text).strip().lower()


def find_known_machine_in_question(question: str, known_records: list[dict[str, str]]) -> Optional[str]:
    """หาชื่อเครื่องที่มีอยู่จริงในคำถามจาก catalog ที่โหลดไว้"""
    if not known_records:
        return None

    q_norm = normalize_for_match(question)
    known_machines = sorted(
        {
            record["machine"].strip()
            for record in known_records
            if record.get("machine") and record["machine"] != "ไม่พบข้อมูล"
        },
        key=len,
        reverse=True,
    )

    for machine in known_machines:
        machine_norm = normalize_for_match(machine)
        if machine_norm and machine_norm in q_norm:
            return machine

    return None


def find_machine_filter_from_question(question: str, known_records: list[dict[str, str]]) -> Optional[str]:
    """หา machine filter แบบ exact หรือแบบคำใบ้บางส่วนที่ยังอยู่ใน catalog จริง"""
    exact_machine = find_known_machine_in_question(question, known_records)
    if exact_machine:
        return exact_machine

    machine_candidate = extract_machine_candidate(question)
    if not machine_candidate:
        return None

    candidate_norm = normalize_for_match(machine_candidate)
    if not candidate_norm:
        return None

    for record in known_records:
        machine_norm = normalize_for_match(record.get("machine", ""))
        if not machine_norm:
            continue
        if candidate_norm in machine_norm or machine_norm in candidate_norm:
            return machine_candidate

    return None


def log_question_event(question: str, source: str = "unknown") -> None:
    """บันทึกคำถามพร้อมวันที่เวลาแบบ CSV แยกโฟลเดอร์รายเดือน"""
    now = datetime.now()
    payload = {
        "timestamp": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "source": source,
        "question": question,
    }
    month_year_folder = LOGS_DIR / now.strftime("%m-%Y")
    log_file = month_year_folder / QUESTION_LOG_FILENAME

    try:
        month_year_folder.mkdir(parents=True, exist_ok=True)
        file_exists = log_file.exists()
        with log_file.open("a", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=["timestamp", "date", "time", "source", "question"],
            )
            if not file_exists:
                writer.writeheader()
            writer.writerow(payload)
    except Exception:
        # ไม่ให้การบันทึก log ทำให้การตอบคำถามล้มเหลว
        pass


def normalize_question_for_catalog_search(question: str) -> str:
    """ตัด label นำหน้าในคำถามเพื่อใช้เทียบกับ catalog โดยตรง"""
    normalized = normalize_for_match(question)
    normalized = re.sub(r"^\[(?:อาการ|สาเหตุ|การแก้ไข|วิธีการแก้ไข)\]\s*[:：]?\s*", "", normalized)
    normalized = re.sub(r"^(?:อาการ|สาเหตุ|การแก้ไข|วิธีการแก้ไข)\s*[:：-]\s*", "", normalized)
    return normalized.strip()


def find_exact_record_from_catalog(question: str, known_records: list[dict[str, str]]) -> Optional[dict[str, str]]:
    """ค้นหา record แบบตรงตัวจาก known catalog เมื่อผู้ใช้พิมพ์ symptom/cause/fix โดยตรง"""
    if not known_records:
        return None

    q_norm = normalize_question_for_catalog_search(question)
    if len(q_norm) < 4:
        return None

    priority_fields = ("cause", "symptom", "fix")
    for field in priority_fields:
        for record in known_records:
            value = normalize_for_match(record.get(field, ""))
            if not value or value == "ไม่พบข้อมูล":
                continue
            if q_norm in value or value in q_norm:
                return record

    return None


def find_prefix_matched_records_from_catalog(
    question: str,
    known_records: list[dict[str, str]],
) -> list[dict[str, str]]:
    """ค้นหา record ที่ symptom ขึ้นต้นเหมือนคำถาม เพื่อคืนหลายผลลัพธ์ที่มี prefix เดียวกัน"""
    if not known_records:
        return []

    q_norm = normalize_question_for_catalog_search(question)
    q_norm = re.sub(r"\s+", " ", q_norm).strip().lower()
    alarm_pos = q_norm.find("alarm")
    if alarm_pos > 0:
        q_norm = q_norm[alarm_pos:]
    if len(q_norm) < 4:
        return []

    machine_filter = find_machine_filter_from_question(question, known_records)
    machine_filter_norm = normalize_for_match(machine_filter or "")

    matched_records: list[dict[str, str]] = []
    seen_records: set[tuple[str, str, str, str]] = set()

    allow_generic_alarm_only = q_norm == "alarm"

    for record in known_records:
        machine = record.get("machine", "")
        symptom = record.get("symptom", "")
        cause = record.get("cause", "")
        fix = record.get("fix", "")

        symptom_norm = normalize_question_for_catalog_search(symptom)
        symptom_norm = re.sub(r"\s+", " ", symptom_norm).strip().lower()
        if not symptom_norm:
            continue

        if machine_filter_norm:
            record_machine_norm = normalize_for_match(machine)
            if machine_filter_norm not in record_machine_norm and record_machine_norm not in machine_filter_norm:
                continue

        if symptom_norm == "alarm" and not allow_generic_alarm_only:
            continue

        if not symptom_norm.startswith(q_norm):
            continue

        record_key = (machine.strip(), symptom.strip(), cause.strip(), fix.strip())
        if record_key in seen_records:
            continue

        seen_records.add(record_key)
        matched_records.append(record)

    return matched_records


def extract_alarm_hints(text: str) -> list[str]:
    """ดึงคำใบ้ที่คล้ายรหัส Alarm เช่น 6050, A000, MPE11 จากคำถาม"""
    normalized = normalize_for_match(text)
    hints: list[str] = []

    direct_alarm = extract_alarm_code(text)
    if direct_alarm:
        hints.append(direct_alarm)

    for token in re.findall(r"[a-z0-9][a-z0-9\-_/]*", normalized):
        compact = token.replace("-", "").replace("_", "").replace("/", "")
        has_digit = any(ch.isdigit() for ch in compact)
        if has_digit and len(compact) >= 3:
            hints.append(token)

    # ลบซ้ำโดยคงลำดับ
    return list(dict.fromkeys(hints))


def extract_search_terms(question: str) -> list[str]:
    """แยกคำค้นแบบยืดหยุ่นและตัดคำทั่วไปที่ไม่มีความหมายเชิงเทคนิค"""
    normalized = normalize_question_for_catalog_search(question)
    terms = re.findall(r"[a-zA-Z0-9ก-๙][a-zA-Z0-9ก-๙\-_/]*", normalized)
    stopwords = {
        "alarm",
        "อาการ",
        "แก้",
        "แก้ยังไง",
        "ยังไง",
        "วิธี",
        "ทำไง",
        "คือ",
        "อะไร",
        "ช่วย",
        "หน่อย",
        "เครื่อง",
        "machine",
    }
    filtered = [term.lower() for term in terms if term.lower() not in stopwords and len(term) >= 2]
    return list(dict.fromkeys(filtered))


def find_flexible_record_from_catalog(question: str, known_records: list[dict[str, str]]) -> Optional[dict[str, str]]:
    """ค้นหา record แบบยืดหยุ่นจาก machine/symptom/cause/fix โดยไม่ใช้การสรุปจากโมเดล"""
    if not known_records:
        return None

    q_norm = normalize_question_for_catalog_search(question)
    if not q_norm:
        return None

    terms = extract_search_terms(question)
    alarm_hints = extract_alarm_hints(question)
    known_machine = find_machine_filter_from_question(question, known_records)
    known_machine_norm = normalize_for_match(known_machine or "")

    best_record: Optional[dict[str, str]] = None
    best_score = 0

    for record in known_records:
        machine = record.get("machine", "")
        symptom = record.get("symptom", "")
        cause = record.get("cause", "")
        fix = record.get("fix", "")

        machine_norm = normalize_for_match(machine)
        symptom_norm = normalize_for_match(symptom)
        cause_norm = normalize_for_match(cause)
        fix_norm = normalize_for_match(fix)
        combined = " ".join([machine_norm, symptom_norm, cause_norm, fix_norm]).strip()

        # ถ้าผู้ใช้ระบุชื่อเครื่องที่รู้จัก ให้ตัดเฉพาะเครื่องเดียวกัน
        if known_machine_norm and known_machine_norm not in machine_norm and machine_norm not in known_machine_norm:
            continue

        score = 0

        if q_norm in symptom_norm:
            score += 120
        elif q_norm in machine_norm:
            score += 90
        elif q_norm in cause_norm or q_norm in fix_norm:
            score += 70
        elif q_norm in combined:
            score += 50

        for hint in alarm_hints:
            if hint and hint in symptom_norm:
                score += 45

        for term in terms:
            if term in symptom_norm:
                score += 25
            elif term in machine_norm:
                score += 15
            elif term in cause_norm or term in fix_norm:
                score += 8

        # ตีตกผลลัพธ์ที่ไม่มีคำร่วมเลย
        if score <= 0:
            continue

        if score > best_score:
            best_score = score
            best_record = record

    return best_record


def extract_alarm_code(text: str) -> Optional[str]:
    """ดึงรหัส Alarm จากคำถามผู้ใช้ เช่น Alarm SERVO MPE 11 -> servo mpe 11"""
    m = re.search(
        r"alarm\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-_/ ]*[A-Za-z0-9])",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None

    alarm_text = re.sub(r"\s+", " ", m.group(1).strip()).lower()
    # กันกรณีผู้ใช้พิมพ์ซ้ำเช่น "alarm: alarm 6050"
    alarm_text = re.sub(r"^(?:alarm|อาการ)\s*[:：-]?\s*", "", alarm_text, flags=re.IGNORECASE).strip()
    return alarm_text or None


def extract_machine_candidate(text: str) -> Optional[str]:
    """
    พยายามดึงชื่อเครื่องที่ผู้ใช้พิมพ์มาในคำถาม
    รองรับรูปแบบ เช่น:
    - BROTHER TC-S2A NC Alarm 5567
    - เครื่อง BROTHER TC-S2A NC Alarm 5567
    - MACHINE: BROTHER TC-S2A NC Alarm 5567
    """
    patterns = [
        r"(?:machine|เครื่อง)\s*[:：]?\s*(.+?)(?=\s*alarm\b|$)",
        r"^(.+?)(?=\s*alarm\b)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        candidate = m.group(1).strip(" :-_\t\n")
        candidate = re.sub(r"^(?:ของ|รุ่น|เครื่อง)\s+", "", candidate, flags=re.IGNORECASE)

        # ต้องมีสาระพอ (ไม่ใช่คำทั่วไปสั้นๆ)
        if len(re.sub(r"[^A-Za-z0-9]", "", candidate)) >= 3:
            return candidate

    return None


def validate_required_machine_and_alarm(
    question: str,
    known_records: Optional[list[dict[str, str]]] = None,
) -> Optional[str]:
    """บังคับให้คำถามต้องมีชื่อเครื่องที่รู้จัก และมี Alarm หรือคำใบ้อาการ"""
    alarm = extract_alarm_code(question)

    if known_records:
        machine = find_known_machine_in_question(question, known_records)
        if not machine:
            return REQUIRE_MACHINE_ALARM_NOTICE

        q_norm = normalize_for_match(question)
        machine_norm = normalize_for_match(machine)
        tail = q_norm.split(machine_norm, 1)[1].strip()
        tail = re.sub(r"^(?:alarm|อาการ)\s*[:：-]?\s*", "", tail, flags=re.IGNORECASE).strip()

        if alarm or len(tail) >= 2:
            return None

        return REQUIRE_MACHINE_ALARM_NOTICE

    return REQUIRE_MACHINE_ALARM_NOTICE


def find_partial_record_from_catalog(question: str, known_records: list[dict[str, str]]) -> Optional[dict[str, str]]:
    """หา record จากชื่อเครื่อง + คำใบ้อาการสั้นๆ เช่น `TOYODA Alarm Servo`"""
    if not known_records:
        return None

    machine_candidate = find_known_machine_in_question(question, known_records)
    if not machine_candidate:
        return None

    q_norm = normalize_for_match(question)
    machine_norm = normalize_for_match(machine_candidate)
    if machine_norm not in q_norm:
        return None

    tail = q_norm.split(machine_norm, 1)[1].strip()
    tail = re.sub(r"^(?:alarm|อาการ)\s*[:：-]?\s*", "", tail, flags=re.IGNORECASE).strip()
    if len(tail) < 2:
        return None

    tail_tokens = [token for token in re.split(r"\s+", tail) if token and token not in {"alarm", "อาการ"}]
    tail_tokens = [token for token in tail_tokens if len(token) >= 2]
    if not tail_tokens:
        return None

    def score_match(symptom_text: str) -> int:
        symptom_norm = normalize_for_match(symptom_text)
        score = 0
        for token in tail_tokens:
            if token in symptom_norm:
                score += len(token)
            else:
                prefix_matches = [word for word in symptom_norm.split() if word.startswith(token) or token.startswith(word)]
                if prefix_matches:
                    score += max(1, len(token) // 2)
        return score

    best_record: Optional[dict[str, str]] = None
    best_score = 0
    for record in known_records:
        record_machine = normalize_for_match(record.get("machine", ""))
        record_symptom = normalize_for_match(record.get("symptom", ""))
        if not record_machine or not record_symptom:
            continue
        if machine_norm not in record_machine and record_machine not in machine_norm:
            continue
        score = score_match(record_symptom)
        if score > best_score:
            best_score = score
            best_record = record

    if best_score > 0:
        return best_record

    return None


def find_best_record_for_machine_alarm(
    question: str,
    known_records: list[dict[str, str]],
) -> Optional[dict[str, str]]:
    """เลือก record ที่ข้อมูลครบที่สุดเมื่อถามแบบระบุเครื่อง + Alarm ชัดเจน"""
    if not known_records:
        return None

    machine = find_known_machine_in_question(question, known_records)
    alarm = extract_alarm_code(question)
    if not machine or not alarm:
        return None

    machine_norm = normalize_for_match(machine)
    candidates: list[dict[str, str]] = []
    for record in known_records:
        record_machine = normalize_for_match(record.get("machine", ""))
        record_symptom = normalize_for_match(record.get("symptom", ""))
        if not record_machine or not record_symptom:
            continue
        if machine_norm not in record_machine and record_machine not in machine_norm:
            continue
        if alarm not in record_symptom:
            continue
        candidates.append(record)

    if not candidates:
        return None

    # เลือก record ที่มีรายละเอียดรวมมากกว่า (สาเหตุ+การแก้ไข) เพื่อกันคำตอบสั้นเกิน
    return max(
        candidates,
        key=lambda r: len((r.get("cause") or "").strip()) + len((r.get("fix") or "").strip()),
    )


def find_all_records_for_machine_alarm(
    question: str,
    known_records: list[dict[str, str]],
) -> list[dict[str, str]]:
    """คืนทุก record ที่ตรงกับ machine + alarm phrase โดยไม่บีบเหลือรายการเดียว"""
    if not known_records:
        return []

    machine = find_known_machine_in_question(question, known_records)
    alarm = extract_alarm_code(question)
    if not machine or not alarm:
        return []

    machine_norm = normalize_for_match(machine)
    alarm_norm = normalize_for_match(alarm)
    if not machine_norm or not alarm_norm:
        return []

    matched_records: list[dict[str, str]] = []
    seen_records: set[tuple[str, str, str, str]] = set()

    for record in known_records:
        record_machine = record.get("machine", "")
        record_symptom = record.get("symptom", "")
        record_cause = record.get("cause", "")
        record_fix = record.get("fix", "")

        record_machine_norm = normalize_for_match(record_machine)
        record_symptom_norm = normalize_for_match(record_symptom)
        if not record_machine_norm or not record_symptom_norm:
            continue

        if machine_norm not in record_machine_norm and record_machine_norm not in machine_norm:
            continue

        if alarm_norm not in record_symptom_norm:
            continue

        record_key = (
            record_machine.strip(),
            record_symptom.strip(),
            record_cause.strip(),
            record_fix.strip(),
        )
        if record_key in seen_records:
            continue

        seen_records.add(record_key)
        matched_records.append(record)

    return matched_records


def validate_question_against_catalog(question: str, known_records: list[dict[str, str]]) -> Optional[str]:
    """
    ตรวจสอบคำถามผู้ใช้กับฐานข้อมูลจริง
    - ถ้าระบุ MACHINE แล้วไม่พบในฐานข้อมูล -> แจ้งไม่มีในฐานข้อมูล
    - ถ้าระบุ Alarm แล้วไม่พบในฐานข้อมูล -> แจ้งไม่มีในฐานข้อมูล
    - ถ้าระบุทั้งคู่ แต่คู่นั้นไม่มีจริง -> แจ้งไม่มีในฐานข้อมูล
    """
    if not known_records:
        return None

    known_machines = sorted(
        {
            record["machine"].strip()
            for record in known_records
            if record.get("machine") and record["machine"] != "ไม่พบข้อมูล"
        }
    )

    q_norm = normalize_for_match(question)
    machine_candidate = extract_machine_candidate(question)
    alarm_in_question = extract_alarm_code(question)

    matched_machines_in_question = [
        machine for machine in known_machines if normalize_for_match(machine) in q_norm
    ]

    if not matched_machines_in_question and machine_candidate:
        candidate_norm = normalize_for_match(machine_candidate)
        if candidate_norm:
            matched_machines_in_question = [
                machine
                for machine in known_machines
                if candidate_norm in normalize_for_match(machine)
                or normalize_for_match(machine) in candidate_norm
            ]

    if machine_candidate and not matched_machines_in_question:
        return NOT_FOUND_NOTICE

    if matched_machines_in_question and alarm_in_question and re.search(r"\d", alarm_in_question):
        preferred_machine = find_known_machine_in_question(question, known_records)
        if preferred_machine and preferred_machine in matched_machines_in_question:
            machine = preferred_machine
        else:
            machine = max(matched_machines_in_question, key=len)

        machine_norm = normalize_for_match(machine)
        machine_tokens = [token for token in re.split(r"\s+", machine_norm) if token]
        is_generic_machine_hint = len(machine_tokens) == 1 and machine_tokens[0].isalpha()

        def machine_matches(record_machine: str) -> bool:
            record_machine_norm = normalize_for_match(record_machine)
            if record_machine_norm == machine_norm:
                return True
            # คำถามแบบแบรนด์กว้าง เช่น "BROTHER alarm 6053"
            # อนุญาตให้ตรงกับรุ่นย่อยที่ขึ้นต้นด้วยแบรนด์เดียวกัน
            if is_generic_machine_hint and machine_norm and machine_norm in record_machine_norm:
                return True
            return False

        matching_alarm_records = [
            record for record in known_records
            if machine_matches(record.get("machine", ""))
            and extract_alarm_code(record.get("symptom", "")) == alarm_in_question
        ]
        if not matching_alarm_records:
            return NOT_FOUND_NOTICE

    return None


def find_exact_record_from_docs(question: str, docs: list[Document]) -> Optional[dict[str, str]]:
    """
    พยายามหา record ที่ตรงกับ Alarm ในคำถามแบบตรงตัว
    เพื่อกันโมเดลสรุปขั้นตอนการแก้ไขจนข้อมูลหาย
    """
    alarm_code = extract_alarm_code(question)
    if not alarm_code:
        return None

    q_lower = question.lower()
    for doc in docs:
        record = parse_record_from_text(doc.page_content)
        symptom_lower = record["symptom"].lower()
        machine_lower = record["machine"].lower()

        # ต้อง match alarm ตรงก่อน
        if alarm_code not in symptom_lower:
            continue

        # ถ้าผู้ใช้ระบุชื่อเครื่องด้วย ให้พยายาม match เครื่องร่วมด้วย
        machine_tokens = [token for token in re.split(r"\s+", machine_lower) if token]
        if machine_tokens:
            overlap = sum(1 for token in machine_tokens if token in q_lower)
            if overlap == 0 and "machine" in q_lower:
                continue

        return record

    return None


def build_prompt() -> ChatPromptTemplate:
    """สร้าง System Prompt ที่บังคับรูปแบบคำตอบอย่างเคร่งครัด"""
    system_prompt = (
        "คุณคือผู้ช่วยช่างซ่อมบำรุงเครื่องจักร\n"
        # "คุณต้องตอบเป็นภาษาไทยล้วนเท่านั้น ยกเว้นคำว่า MACHINE ที่ต้องคงตาม format\n"
        "ให้ใช้ข้อมูลจากบริบทที่ให้มาเท่านั้น ห้ามแต่งข้อมูลเพิ่มเอง\n"
        "ห้ามทักทาย ห้ามเกริ่นนำ ห้ามอธิบายเพิ่ม ห้ามใส่ markdown ห้ามใส่ code block\n"
        "กฎเหล็ก: ต้องตอบกลับเฉพาะ format นี้เท่านั้น และห้ามมีข้อความอื่นนอกเหนือจากนี้:\n"
        "MACHINE: [ชื่อรุ่นเครื่องจักร]\n"
        "อาการ: [อาการ/รหัส Alarm]\n"
        "สาเหตุ: [ระบุสาเหตุ]\n"
        "การแก้ไข: [ระบุขั้นตอนการแก้ไข]\n\n"
        "ถ้าไม่พบข้อมูลที่ตรง ให้ตอบคำว่า 'ไม่พบข้อมูล' ในแต่ละช่อง\n"
        "ถ้าพบชื่อรุ่นเครื่องจักรและ Alarm ที่ตรงกันให้ทำมาตอบทั้งหมด ถึงสาเหตุจะไม่ตรงกัน\n\n"
        "บริบทข้อมูล:\n{context}"
    )

    return ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", "คำถามผู้ใช้: {question}"),
        ]
    )


def answer_question(
    question: str,
    retriever,
    llm: ChatOllama,
    prompt: ChatPromptTemplate,
    known_records: Optional[list[dict[str, str]]] = None,
    query_source: str = "unknown",
) -> str:
    """ตอบจากข้อมูลคู่มือโดยคัดลอกข้อความจาก record ต้นฉบับแบบตรงตัว"""
    log_question_event(question=question, source=query_source)

    if known_records:
        invalid_message = validate_question_against_catalog(question=question, known_records=known_records)
        if invalid_message:
            return invalid_message

    if known_records:
        machine_alarm_records = find_all_records_for_machine_alarm(question=question, known_records=known_records)
        if machine_alarm_records:
            alarm_phrase = extract_alarm_code(question) or ""
            is_specific_alarm_code = bool(re.search(r"\d", alarm_phrase))

            if len(machine_alarm_records) == 1:
                return format_answer_from_record(machine_alarm_records[0])

            if is_specific_alarm_code:
                best_machine_alarm_record = max(
                    machine_alarm_records,
                    key=lambda r: len((r.get("cause") or "").strip()) + len((r.get("fix") or "").strip()),
                )
                return format_answer_from_record(best_machine_alarm_record)

            return "\n\n".join(format_answer_from_record(record) for record in machine_alarm_records)

        prefix_matched_records = find_prefix_matched_records_from_catalog(question=question, known_records=known_records)
        if prefix_matched_records:
            return "\n\n".join(format_answer_from_record(record) for record in prefix_matched_records)

        exact_catalog_record = find_exact_record_from_catalog(question=question, known_records=known_records)
        if exact_catalog_record:
            return format_answer_from_record(exact_catalog_record)

        partial_catalog_record = find_partial_record_from_catalog(question=question, known_records=known_records)
        if partial_catalog_record:
            return format_answer_from_record(partial_catalog_record)

        flexible_catalog_record = find_flexible_record_from_catalog(question=question, known_records=known_records)
        if flexible_catalog_record:
            return format_answer_from_record(flexible_catalog_record)

    try:
        if retriever is None:
            return NOT_FOUND_NOTICE

        docs = retriever.invoke(question)

        # กรอง docs ให้ตรงเครื่องที่ถาม ป้องกัน LLM ตอบข้ามเครื่อง
        if known_records:
            machine_in_q = find_machine_filter_from_question(question, known_records)
            if machine_in_q:
                machine_norm = normalize_for_match(machine_in_q)
                same_machine_docs = []
                for d in docs:
                    # ดึงชื่อเครื่องจาก MACHINE field โดยตรงแทนการตรวจสอบ 200 ตัวแรก
                    parsed = parse_record_from_text(d.page_content)
                    doc_machine_norm = normalize_for_match(parsed.get("machine", ""))
                    if machine_norm in doc_machine_norm or doc_machine_norm in machine_norm:
                        same_machine_docs.append(d)
                
                # ถ้าเจอ docs ของเครื่องเดียวกัน ใช้เท่านั้น มิฉะนั้นใช้เอกสารที่เรียกคืนมาทั้งหมด
                if same_machine_docs:
                    docs = same_machine_docs

        # ถ้าจับคู่ Alarm ได้แบบตรงตัว ให้ตอบจากข้อมูลต้นฉบับทันที
        # เพื่อคงรายละเอียดการแก้ไขหลายบรรทัดไม่ให้ถูกย่อ
        exact_record = find_exact_record_from_docs(question=question, docs=docs)
        if exact_record:
            return format_answer_from_record(exact_record)

        # ตอบจาก record ที่ parse จากเอกสารที่ค้นคืนมา เพื่อคงข้อความต้นฉบับ
        doc_records = [parse_record_from_text(doc.page_content) for doc in docs if doc.page_content.strip()]
        best_doc_record = find_flexible_record_from_catalog(question=question, known_records=doc_records)
        if best_doc_record:
            return format_answer_from_record(best_doc_record)

        return NOT_FOUND_NOTICE
    except Exception:
        return NOT_FOUND_NOTICE


def initialize_rag_components() -> dict[str, object]:
    """
    เตรียมองค์ประกอบ RAG กลางสำหรับทั้งโหมด Terminal และ Web UI
    คืนค่าทุกอย่างใน dict เพื่อเรียกใช้ต่อได้สะดวก
    """
    manual_files = discover_manual_files()
    all_docs: list[Document] = []

    for file_path in manual_files:
        raw_text = load_manual_text(file_path)
        chunks = split_by_machine_record(raw_text)
        all_docs.extend(build_documents(chunks, source_file=file_path))

    if not all_docs:
        raise RuntimeError("ไม่พบข้อมูลหลังการ split กรุณาตรวจสอบรูปแบบไฟล์คู่มือทั้งหมด")

    files_signature = compute_files_signature(manual_files)
    old_signature = load_manifest_signature()
    index_needs_rebuild = FORCE_REBUILD_INDEX or (old_signature != files_signature) or (not CHROMA_DIR.exists())

    vectorstore = prepare_vectorstore(all_docs, files_signature=files_signature)
    if index_needs_rebuild:
        save_manifest_signature(
            files_signature=files_signature,
            files=manual_files,
            docs_count=len(all_docs),
        )

    retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 3})

    llm = ChatOllama(
        model=OLLAMA_LLM_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=0,
    )
    prompt = build_prompt()

    known_records = [
        parse_record_from_text(doc.page_content)
        for doc in all_docs
        if doc.page_content.strip()
    ]

    return {
        "retriever": retriever,
        "llm": llm,
        "prompt": prompt,
        "known_records": known_records,
        "manual_files": manual_files,
        "index_needs_rebuild": index_needs_rebuild,
    }


def main() -> None:
    """Entry point ของโปรแกรมแชทบน Terminal"""
    print("กำลังเตรียมระบบ Local RAG...")
    components = initialize_rag_components()
    retriever = components["retriever"]
    llm = components["llm"]
    prompt = components["prompt"]
    known_records = components["known_records"]
    manual_files = components["manual_files"]
    index_needs_rebuild = components["index_needs_rebuild"]

    print("ระบบพร้อมใช้งานแล้ว")
    print("ไฟล์คู่มือที่โหลด:")
    for file_path in manual_files:
        print(f"- {file_path}")
    if index_needs_rebuild:
        print("สถานะดัชนี: มีการสร้าง/อัปเดตดัชนีใหม่")
    else:
        print("สถานะดัชนี: ใช้ดัชนีเดิม (ไฟล์คู่มือไม่เปลี่ยน)")
    print("พิมพ์คำถามเกี่ยวกับ Alarm/อาการเครื่องจักรได้เลย (พิมพ์ 'exit' เพื่อออก)\n")

    while True:
        user_input = input("You> ").strip()

        if not user_input:
            continue

        if user_input.lower() == "exit":
            print("จบการทำงาน")
            break

        try:
            answer = answer_question(
                question=user_input,
                retriever=retriever,
                llm=llm,
                prompt=prompt,
                known_records=known_records,
                query_source="terminal",
            )
            print(f"\n{answer}\n")
        except Exception as exc:
            # กรณีเกิดปัญหาระหว่างเรียกโมเดลหรือ retrieval
            print("\nMACHINE: ไม่พบข้อมูล")
            print("อาการ: ไม่พบข้อมูล")
            print("สาเหตุ: ไม่พบข้อมูล")
            print(f"การแก้ไข: เกิดข้อผิดพลาดในการประมวลผล ({exc})\n")


if __name__ == "__main__":
    main()
