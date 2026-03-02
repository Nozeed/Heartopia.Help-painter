# 🎨 Heartopia.Help-painter

เครื่องมือช่วยแปลงรูปภาพให้กลายเป็น **Pixel Art** สำหรับไกด์การวาดภาพในเกม **Heartopia** โดยเฉพาะ! ช่วยให้คุณเลือกสีจาก Palette ในเกมได้แม่นยำและวางแผนการวาดได้ง่ายขึ้น
โปรแกรมแปลไทยให้แล้ว ได้ใช้งานได้แบบไม่งงกัน
---

## ✨ คุณสมบัติ (Features)
* **Image to Pixel Art:** แปลงรูปภาพทั่วไปให้เป็นสไตล์พิกเซล
* **Game Palette Matching:** รองรับการเทียบสีให้ตรงกับสีที่มีในเกม Heartopia
* **Custom Grid:** กำหนดขนาดตาราง (Grid) ได้ตามต้องการ
* **Preview Mode:** ดูตัวอย่างภาพก่อนนำไปวาดจริง

---

## Requirements
- Python 3.10 ขึ้นไป
- Pillow (PIL)
- CustomTkinter (สำหรับหน้าจอ UI)
- Windows 10 (ทุก build ตั้งแต่ 1809 ขึ้นไป โดยเฉพาะ 21H2, 22H2) → ใช้ได้แน่นอน
- Windows 11 (ทุก build เช่น 21H2, 22H2, 23H2, 24H2) → ใช้ได้ดีที่สุด (แนะนำเพราะ Python ล่าสุดรองรับดี)

## ดูวิดีโอสอนใช้งาน [YOUTUBE](https://youtu.be/nr_-wqowNoM?si=U9XXHyDxfqi201iv)
[https://youtu.be/nr_-wqowNoM?si=U9XXHyDxfqi201iv](https://youtu.be/nr_-wqowNoM?si=U9XXHyDxfqi201iv)

## 🛠️ การติดตั้งบน Windows Windows (Installation)

แนะนำให้รันผ่าน **Virtual Environment** เพื่อป้องกัน Library ตีกับโปรเจกต์อื่นครับ

### เตรียมโฟลเดอร์และ Virtual Environment
เปิด **PowerShell** (Run as administrator) ในโฟลเดอร์โปรเจกต์แล้วรันคำสั่ง:
```powershell
# สร้างสภาพแวดล้อมจำลอง
python -m venv .venv

# เปิดใช้งาน (Activate)
.\.venv\Scripts\Activate.ps1
# หาก Windows บล็อกการรันไฟล์ .ps1
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# ติดตั้ง Library ที่จำเป็น
pip install -r requirements.txt
```

# 🍎 Heartopia Help Painter — macOS Installation Guide

This guide explains how to install and run **Heartopia Help Painter** on macOS (Intel & Apple Silicon: M1 / M2 / M3).

---

## ✅ Requirements

Before starting, make sure you have:

* macOS 11+ (Big Sur or newer recommended)
* Internet connection
* Terminal access
* Git installed
* Python 3.10 or newer

---

## 1️⃣ Install Homebrew (if not installed)

Homebrew is the recommended package manager for macOS.

Open **Terminal** and run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Verify installation:

```bash
brew --version
```

---

## 2️⃣ Install Python

Install Python using Homebrew:

```bash
brew install python
```

Check Python version:

```bash
python3 --version
```

---

## 3️⃣ Install Git (if needed)

```bash
brew install git
```

Verify:

```bash
git --version
```

---

## 4️⃣ Clone the Repository

```bash
git clone https://github.com/Nozeed/Heartopia.Help-painter.git
cd Heartopia.Help-painter
```

---

## 5️⃣ Create Virtual Environment (Recommended)

Create a Python virtual environment:

```bash
python3 -m venv venv
```

Activate it:

```bash
source venv/bin/activate
```

You should now see `(venv)` in your terminal.

---

## 6️⃣ Install Dependencies

Install required packages:

```bash
pip install -r requirements.txt
```

---

## 7️⃣ Run the Application

Start the app using:

```bash
python src/heartopia_painter/app.py
```

If your project structure differs:

```bash
python app.py
```

---

## ⚠️ Troubleshooting

### tkinter not found (GUI not opening)

Install Tk support:

```bash
brew install python-tk
```

---

### Pillow or image library errors

```bash
pip install --upgrade pillow
```

---

### Apple Silicon (M1/M2/M3) compatibility issues

Try reinstalling dependencies:

```bash
arch -arm64 pip install -r requirements.txt
```

---

## ✅ Optional: Deactivate Virtual Environment

When finished:

```bash
deactivate
```

---

## 🎉 Done!

Heartopia Help Painter should now be running on macOS.

---


ทำตามขั้นตอนสร้าง venv → pip install -r requirements.txt → python main.py
ถ้า PowerShell บล็อก .ps1 ให้รัน "Set-ExecutionPolicy RemoteSigned -Scope CurrentUser" ครั้งเดียว

## วิธีแก้สีเพี้ยนวาดไม่ตรง
[https://www.youtube.com/watch?v=7ReYhclehEA](https://www.youtube.com/watch?v=7ReYhclehEA)

## ข้อมูลอัพเดทแก้ไข
### Update 01/03/26
- ลบขนาดรูป 9:16 ออกมีบัค ใช้ได้แค่ 16:9 กับ 1:1
### Update 28/02/26
- แก้การซูมตอนกด scroll mouse ขึ้นตอนเลือก พื้นที่ Canvas.. ให้ซูมได้ สูงสุด x12
- แก้ Overlay ให้มองง่าย ได้ลากรูปง่ายและตรงกับเกมมากขึ้น

ที่มา : [https://github.com/PckyDev/Heartopia-Image-Painter](https://github.com/PckyDev/Heartopia-Image-Painter)
### โปรแกรมตัวนี้ผมนำมาแก้ไขและแปลให้ใช้งานได้ง่ายขึ้น

### ใครโลกสวยให้ไปแจ้งผู้พัฒนาเกมครับไม่ต้องมาร้องงอแงดิ้นแถวนี้
- ส่วนใครกลัวโดนแบนแนะนำอย่าใช้ครับ :)
- สุดท้าย งดมาม่านะครับ แค่โปรแกรมช่วยวาด ไม่ใช่ Hack สะหน่อยงอแงไปได้
