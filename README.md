# Local RAG Maintenance Assistant (Ollama + LangChain + Chroma)

ระบบถาม-ตอบข้อมูลซ่อมบำรุงเครื่องจักรแบบ Local RAG

- ใช้ Ollama รันโมเดลบนเครื่อง
- ใช้ LangChain เป็น orchestration
- ใช้ Chroma เป็น vector database
- รองรับทั้งโหมด Terminal และ Web UI (Streamlit)
- รองรับหลายไฟล์คู่มือในโฟลเดอร์เดียว (.txt และ .docx)

## Features

- แยกข้อมูลเป็นเคสด้วยตัวคั่น `=== END===` แบบ strict (1 เคส = 1 chunk)
- บังคับรูปแบบคำตอบ:
  - MACHINE
  - อาการ
  - สาเหตุ
  - การแก้ไข
- รองรับ re-index อัตโนมัติเมื่อไฟล์คู่มือเปลี่ยน
- รองรับธีมสว่าง/มืดในหน้าเว็บ

## Project Structure

- `rag_chat_local.py` : แกนหลัก RAG + โหมด Terminal
- `web_chat_ui.py` : Web UI ด้วย Streamlit
- `maintenance_log.txt` : ไฟล์คู่มือหลัก
- `manuals/` : โฟลเดอร์คู่มือเพิ่มเติม (.txt, .docx)
- `requirements.txt` : รายการ dependencies

## Prerequisites

1. ติดตั้ง Python 3.10+
2. ติดตั้ง Ollama และรัน service
3. โหลดโมเดลที่ต้องใช้ เช่น

```bash
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

## Installation

```bash
pip install -r requirements.txt
```

## Run (Terminal Chat)

```bash
python rag_chat_local.py
```

พิมพ์ `exit` เพื่อออกจากโปรแกรม

## Run (Web UI)

```bash
streamlit run web_chat_ui.py
```

จากนั้นเปิด URL ที่ Streamlit แสดง (เช่น http://localhost:8501)

## Data Format

ตัวอย่างรูปแบบข้อมูลในไฟล์คู่มือ:

```text
=== MACHINE: BROTHER TC-S2A NC ===
[อาการ]Alarm 5567
[สาเหตุ]ท่อตัน2
[การแก้ไข]
      1.ล้างท่อ2
      2.ล้างน้ำ
=== END===
```

หมายเหตุสำหรับไฟล์ Word (.docx):

- รองรับไฟล์ `.docx` โดยตรง หากเนื้อหาในเอกสารยังอยู่ในรูปแบบเดียวกับด้านบน
- แนะนำให้คง token สำคัญให้ตรงเดิม: `=== MACHINE: ... ===`, `[อาการ]`, `[สาเหตุ]`, `[การแก้ไข]`, `=== END===`

## Notes

- ถ้าต้องการบังคับ rebuild index ทุกครั้ง:

Windows PowerShell:

```powershell
$env:FORCE_REBUILD_INDEX = "true"
python rag_chat_local.py
```

- ปกติระบบจะ rebuild เฉพาะเมื่อไฟล์คู่มือมีการเปลี่ยนแปลง

## GitHub Upload Suggestion

แนะนำ commit เฉพาะไฟล์ซอร์สและข้อมูลคู่มือ:

- `rag_chat_local.py`
- `web_chat_ui.py`
- `requirements.txt`
- `maintenance_log.txt`
- `manuals/`
- `README.md`
- `.gitignore`

ไม่ควรอัปไฟล์ environment/ไฟล์ generate ขึ้น repo เช่น `venv11/`, `chroma_db/`, `__pycache__/`
