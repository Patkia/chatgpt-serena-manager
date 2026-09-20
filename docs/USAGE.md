# วิธีใช้งาน Serena Manager

ตารางหลักของ Serena Manager แสดง launcher project ที่ค้นพบในเครื่อง

| Column | ความหมาย |
| --- | --- |
| Project | ชื่อที่ใช้เรียก project/launcher |
| Project Path | folder local ที่ส่งให้ Serena |
| Serena | สถานะ Serena MCP ในเครื่อง |
| Tunnel | สถานะ Secure MCP Tunnel |
| MCP Port | Port ที่ Serena MCP รับการเชื่อมต่อ |
| Health Port | Port สำหรับตรวจสุขภาพของ Tunnel |
| Status | สถานะรวมของ project |

## ความหมายของสถานะ

### RUNNING

Serena และ Tunnel ทำงานทั้งคู่ และ health endpoint ของ Tunnel พร้อมใช้งาน

### STOPPED

project ยังไม่ได้เปิด และ Port ที่คาดว่าจะใช้ว่างอยู่

### PARTIAL

Serena หรือ Tunnel ทำงานเพียงบางส่วน ให้ตรวจสองคอลัมน์นั้นแยกกัน

### ERROR

launcher อาจอ่านค่าไม่ได้, Port มี process อื่นใช้ หรือ readiness ยังไม่ผ่าน

## ปุ่มในหน้าต่าง

| ปุ่ม | การทำงาน |
| --- | --- |
| Refresh | ตรวจสถานะทุก project ใหม่ โดยไม่ block หน้าต่าง |
| Start | เปิด launcher แบบ hidden ของ project ที่เลือก |
| Stop | เรียก Stop launcher แบบจำกัดเฉพาะ project ที่เลือก |
| Restart | Stop แล้ว Start project ที่เลือกใหม่ |
| Debug | เปิด debug launcher พร้อม console output |
| Log | เปิด launcher log ของ project ที่เลือก |
| Stop All | Stop project ทั้งหมดที่รายงานว่า RUNNING หรือ PARTIAL |
| `+` | เพิ่ม project ใหม่ที่ Serena Manager ดูแล |
| `X` | ลบเฉพาะ setup ที่ Manager สร้างและยืนยัน ownership ได้ โดยไม่ลบ source project |

สามารถกดเมาส์ซ้ายค้างแล้วลากแถวเพื่อเรียงลำดับ project ได้ ลำดับจะถูกเก็บไว้ใน application-data ของผู้ใช้ และไม่ถูกแชร์ผ่าน Git

## Workflow ที่แนะนำ

1. เปิด Serena Manager
2. เลือกและ Start project ที่ต้องการ
3. รอให้เห็น `RUNNING / RUNNING / RUNNING`
4. เปิด ChatGPT
5. ใช้ Custom MCP Plugin ของ project นั้น
6. ให้ ChatGPT เช็ก Active project
7. เริ่มอ่านหรือแก้ code
8. Stop เมื่อเลิกใช้

ก่อนเชื่อม ChatGPT ให้กด Refresh และยืนยันว่า `Serena = RUNNING`, `Tunnel = RUNNING` และ `Status = RUNNING`
