# MT Assistant AI

ระบบ AI สำหรับตอบคำถามเกี่ยวกับการซ่อมบำรุงเครื่องจักร โดยค้นหาข้อมูลจากคู่มือในโฟลเดอร์ `manuals/` และใช้ Ollama ทำงานบนเครื่องของผู้ใช้

## สิ่งที่ต้องติดตั้ง

- Python 3.12
- Ollama
- โมเดล `bge-m3`

## วิธีติดตั้ง

### 1. ติดตั้ง Python 3.12

ดาวน์โหลดและติดตั้ง Python 3.12 จาก [python.org](https://www.python.org/downloads/)

ตรวจสอบเวอร์ชัน:

```bash
python --version
```

ควรแสดงผลเป็น `Python 3.12.x`

### 2. ดาวน์โหลดโปรเจกต์

เปิด Terminal หรือ Command Prompt แล้วเข้าไปยังโฟลเดอร์โปรเจกต์:

```bat
cd path\to\AI-Chat-Bot-main
```

### 3. สร้าง Virtual Environment

```bat
python -m venv .venv
.venv\Scripts\activate
```

### 4. ติดตั้ง Python packages

```bat
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## ติดตั้ง Ollama และโมเดล AI

ดาวน์โหลด Ollama จาก [ollama.com/download](https://ollama.com/download) และติดตั้งให้เรียบร้อย

ตรวจสอบว่า Ollama ติดตั้งสำเร็จ:

```bat
ollama --version
```

ดาวน์โหลดโมเดล `bge-m3`:

```bat
ollama pull bge-m3
```

ทดสอบโมเดล:

```bat
ollama run bge-m3
```

ปิดหน้าทดสอบด้วย `Ctrl+C` แล้วตรวจสอบให้แน่ใจว่า Ollama ยังทำงานอยู่ก่อนเปิดโปรแกรม

## เตรียมคู่มือ

นำไฟล์คู่มือเครื่องจักรนามสกุล `.txt` หรือ `.docx` ไปใส่ไว้ในโฟลเดอร์:

```text
manuals/
```

เมื่อเปิดโปรแกรม ระบบจะอ่านคู่มือและสร้างฐานข้อมูลค้นหาให้อัตโนมัติ

## วิธีใช้งาน

### วิธีที่ 1: รันด้วย `python app.py`

ตรวจสอบว่าอยู่ในโฟลเดอร์โปรเจกต์และเปิด Virtual Environment แล้ว จากนั้นรัน:

```bat
python app.py
```

เปิดเว็บไซต์ใน Browser:

```text
http://localhost:5000/qa
```

หน้า Dashboard อยู่ที่:

```text
http://localhost:5000/dashboard
```

หยุดโปรแกรมด้วย `Ctrl+C`

### วิธีที่ 2: สร้างและใช้งาน `Run.bat`

สร้างไฟล์ชื่อ `Run.bat` ในโฟลเดอร์เดียวกับ `app.py` แล้วใส่ข้อความนี้:

```bat
@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python app.py
pause
```

ดับเบิลคลิกไฟล์ `Run.bat` เพื่อเริ่มโปรแกรม จากนั้นเปิด `http://localhost:5000/qa`

ถ้ายังไม่ได้สร้าง Virtual Environment ให้สร้างก่อนด้วยคำสั่ง:

```bat
python -m venv .venv
```

แล้วติดตั้ง packages ตามขั้นตอนด้านบน

## การใช้งานหน้าเว็บ

1. เปิด `http://localhost:5000/qa`
2. พิมพ์ชื่อเครื่องจักร, Alarm หรืออาการที่ต้องการค้นหา
3. ระบบจะแสดงข้อมูลเครื่อง อาการ สาเหตุ และวิธีแก้ไขจากคู่มือ
4. หากต้องการเพิ่มคู่มือ ให้ใช้เมนูอัปโหลดในหน้าเว็บ

ระบบรองรับไฟล์ `.txt` และ `.docx` เท่านั้น

## หมายเหตุ

- การเริ่มใช้งานครั้งแรกอาจใช้เวลานาน เนื่องจากระบบต้องสร้างฐานข้อมูลจากคู่มือ
- ต้องเปิด Ollama ก่อนใช้งานโปรแกรม
- หากแก้ไขหรือเพิ่มไฟล์ใน `manuals/` ให้ปิดแล้วเปิดโปรแกรมใหม่
- ข้อมูลคำตอบขึ้นอยู่กับเนื้อหาในคู่มือ ควรตรวจสอบขั้นตอนความปลอดภัยก่อนทำงานกับเครื่องจักรจริง
