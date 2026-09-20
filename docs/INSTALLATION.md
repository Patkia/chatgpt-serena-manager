# การติดตั้งบน Windows

เอกสารนี้อธิบายการเตรียมเครื่องสำหรับใช้ Serena Manager กับ local project บน Windows

## 1. สิ่งที่ต้องติดตั้งก่อน

ติดตั้ง Python 3 ที่มี Tkinter และ Python Launcher แล้วตรวจสอบด้วยคำสั่ง:

```powershell
py -3 --version
pyw.exe -3 --version
```

ติดตั้ง Node.js สำหรับ Language Server ที่ต้องใช้ JavaScript/TypeScript แล้วตรวจสอบ:

```cmd
node -v
npm -v
where node
```

ติดตั้ง Serena ตามเอกสารของ Serena และตรวจว่าอยู่บน `PATH`:

```powershell
serena --help
```

ติดตั้ง Secure MCP Tunnel client ที่ออกให้สำหรับ account ของคุณ เก็บตัว executable และ credential ไว้นอก repository เสมอ ถ้าจะ clone repository แนะนำให้ติดตั้ง Git ด้วย

## 2. Clone หรือวาง Serena Manager

วาง Serena Manager ไว้ในโฟลเดอร์ tools โดยปกติ Manager จะค้นหา launcher folders ที่เป็น sibling กัน ตัวอย่าง:

```text
%USERPROFILE%\Tools\Serena-manager
%USERPROFILE%\Tools\Serena-example-project
```

หากใช้ตำแหน่งอื่น ให้ตั้งค่า `SERENA_TOOLS_ROOT` เป็นโฟลเดอร์ที่เก็บ launcher directories ชื่อ `Serena-*` ทั้งหมด ก่อนเปิด Manager

## 3. ตั้งค่า Credential แบบ local-only

สร้าง control-plane API key file บนเครื่องของคุณเอง ถ้า tunnel client ใช้ DPAPI ให้เข้ารหัสสำหรับ Windows account ของคุณ

launcher ปัจจุบันใช้ convention นี้:

```text
%USERPROFILE%\Tools\control-plane-api-key.dpapi
```

- credential ต้องอยู่บนเครื่องเท่านั้น
- ห้ามใส่ไฟล์นี้ใน repository
- ห้าม paste API key ลง launcher, README, issue หรือ commit

repository ควรมีเฉพาะ code และ template ที่ปลอดภัยต่อการเผยแพร่

## 4. ติดตั้ง Tunnel client ในเครื่อง

Add Project flow ต้องใช้ tunnel executable ในเครื่อง โดย convention ปัจจุบันคือ:

```text
%USERPROFILE%\Tools\tunnel-client-v0.0.14-windows-amd64\tunnel-client.exe
```

หากตำแหน่งติดตั้งต่างออกไป ให้ปรับ local setup ของคุณนอก version control และอย่า commit profile เฉพาะ account จาก `%APPDATA%\tunnel-client`

Serena Manager ใช้ Python standard library จึงไม่มี Python package เพิ่มที่ต้องติดตั้งจาก `requirements.txt`

## 5. เปิด Serena Manager

ดับเบิลคลิก:

```text
Start-Serena-Manager.cmd
```

ไฟล์นี้เรียก hidden VBS launcher จึงไม่ควรมี Command Prompt ค้างอยู่

หาก Manager ไม่เปิด ให้รันจาก PowerShell เพื่อดู error:

```powershell
py -3 .\Serena-Manager.py
```

## 6. เพิ่ม Project แรก

1. กดปุ่ม `+` ใน Serena Manager
2. เลือก folder source project ที่มีอยู่แล้ว
3. ตั้งชื่อ project และใส่ Tunnel ID ของ project นั้น
4. ตรวจ launcher path และ port ที่ระบบเลือก
5. กด Create
6. เลือก project ที่สร้างแล้ว แล้วกด Start

Manager จะสร้าง Serena metadata, launcher folder และ tunnel profile ในเครื่อง พร้อมตรวจ setup ที่สร้างขึ้น

project config ที่สร้างใหม่เปิดสิทธิ์เขียนได้ ควรเริ่มจาก test repository และตรวจไฟล์ที่สร้างก่อนใช้กับงานสำคัญ

## 7. เชื่อม ChatGPT

ก่อน Create หรือใช้งาน Custom MCP Plugin ต้อง Start project ก่อน แล้วตรวจให้เห็น:

```text
Serena = RUNNING
Tunnel = RUNNING
Status = RUNNING
```

หากยังไม่ Start อาจพบ `Error creating connector` ตอนสร้าง Plugin
