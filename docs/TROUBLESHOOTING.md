# แก้ปัญหาที่พบบ่อย

## Serena RUNNING แต่ Tunnel STOPPED / Status PARTIAL

สถานะนี้หมายความว่า Serena MCP ขึ้นแล้ว แต่ Secure MCP Tunnel ยังไม่พร้อม สาเหตุที่ควรตรวจมีดังนี้:

- MCP อาจยังไม่ ready
- tunnel profile อาจไม่มีหรือ syntax/path ไม่ถูกต้อง
- Health Port อาจชนกับ process อื่น
- tunnel client อาจยังติดตั้งไม่ครบหรือเริ่มทำงานไม่สำเร็จ

เปิด log ของ project จากปุ่ม Log แล้วตรวจ `launcher.log`, `tunnel.log` และ `tunnel.stderr.log` ในเครื่อง อย่า paste credential หรือ Tunnel ID เต็มลงใน public issue

## Node.js not found

Language Server บางตัวต้องใช้ Node.js ตรวจสอบด้วย:

```cmd
node -v
npm -v
where node
```

หากเพิ่งติดตั้ง Node.js:

1. ปิด Serena Manager
2. เปิด Serena Manager ใหม่
3. Start project ใหม่

## Language server error

เมื่อ Language Server มีปัญหา semantic search, refactor หรือ diagnostics อาจทำงานได้ไม่เต็มที่ แต่การอ่าน/เขียนไฟล์ผ่าน Serena ยังอาจใช้ได้ตาม config ของ project

ตรวจว่า runtime ที่ Language Server ต้องใช้ติดตั้งอยู่แล้ว และดู Serena log ของ project เพื่อหาข้อความ error ที่เกี่ยวข้อง

## Plugin สร้างไม่ได้ / Error creating connector

Start project ก่อน แล้วรอให้สถานะเป็น:

```text
Serena = RUNNING
Tunnel = RUNNING
Status = RUNNING
```

ตรวจ Active project, tunnel profile และ Custom MCP Plugin configuration จากนั้นสร้าง Plugin โดยใช้ Tunnel ID ของตัวเองเท่านั้น

## Port ชน

ตรวจทั้งสองกรณี:

- Port มี process อื่นกำลังใช้อยู่ใน OS
- Port ถูกจองไว้ใน launcher ของ project อื่น แม้ project นั้นยัง STOPPED

อย่าใช้ MCP Port หรือ Health Port คู่เดียวกับ project อื่น

## Tunnel profile error

tunnel profile อยู่ที่:

```text
%APPDATA%\tunnel-client
```

ตรวจ syntax ของ profile และ path credential ที่ launcher คาดไว้ เก็บทั้ง profile และ credential ไว้นอก Git และห้ามแชร์ credential หรือ Tunnel ID แบบ public

## Log อยู่ที่ไหน

แต่ละ launcher folder มีโฟลเดอร์ `logs` สำหรับเก็บ:

- `launcher.log` สำหรับเหตุการณ์ Start/Stop/Restart
- `serena.log` สำหรับ output ของ Serena
- `tunnel.log` สำหรับ output ของ Tunnel
- `tunnel.stderr.log` สำหรับ error output ของ Tunnel

ใช้ปุ่ม Log ใน Serena Manager เพื่อเปิด `launcher.log` ของ project ที่เลือก

## Manager ไม่เปิด

รันคำสั่งนี้จาก folder Serena Manager:

```powershell
py -3 .\Serena-Manager.py
```

ตรวจว่า Python มี Tkinter และ `pyw.exe -3 --version` ทำงานได้ hidden launcher ตั้งใจไม่แสดง console output
