# ความปลอดภัย

Custom MCP และ Serena สามารถอ่านไฟล์, แก้ไฟล์ และรันคำสั่งใน project ที่เชื่อมไว้ได้ จึงควรเพิ่มเฉพาะ project ที่ไว้ใจ และตรวจทุกการเปลี่ยนแปลงก่อนใช้งานหรือส่งต่อ

## ห้าม Commit สิ่งเหล่านี้

- API key หรือ access token
- DPAPI credential (`*.dpapi`)
- Tunnel ID จริง, tunnel profile ที่สร้างแล้ว หรือ endpoint เฉพาะ account
- `.env`
- private key หรือ certificate
- logs, backup, crash trace หรือ local project path ที่มีข้อมูลส่วนตัว

## Credential ควรอยู่ที่ไหน

credential ควรอยู่ local-only บนเครื่องของคุณ ไม่อยู่ใน repository, README, issue หรือ commit

`.gitignore` ช่วยกัน runtime artifacts ที่พบบ่อย แต่ไม่สามารถรับประกันได้ทั้งหมด ตรวจ `git status` ทุกครั้งก่อน commit

## ก่อน Push ขึ้น GitHub

ตรวจไฟล์ที่กำลังจะเผยแพร่:

```cmd
git status --short
git diff --cached --check
```

ค้นคำที่เสี่ยง เช่น:

- `C:\Users\`
- `tunnel_`
- `api_key`
- `Bearer `
- `BEGIN PRIVATE KEY`

คำเหล่านี้อาจปรากฏใน code หรือ docs ได้ แต่ต้องไม่มีค่าจริง, credential จริง หรือข้อมูลส่วนตัวติดไปด้วย

## การเข้าถึง local code

ใช้สิทธิ์เท่าที่จำเป็น จำกัด filesystem permissions หากทำได้, เชื่อมเฉพาะ repository ที่ไว้ใจ และ Stop Tunnel เมื่อเลิกใช้งาน

## การรายงาน Security issue

ห้ามเปิด public issue ที่มี credential, Tunnel ID, local project path หรือ reproduction log ที่ไม่ได้ sanitize

เมื่อมีช่องทาง private contact ของ maintainer ให้ใช้ช่องทางนั้นสำหรับรายงานปัญหาด้านความปลอดภัย ก่อนมีช่องทางดังกล่าว ให้ส่งเพียงคำอธิบายที่ผ่านการ sanitize และขอวิธีติดต่อแบบ private
