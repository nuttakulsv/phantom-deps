# phantom-deps

[English](README.md) · จากคนทำ [safe-migrations](https://github.com/nuttakulsv/safe-migrations) และ [honest-tests](https://github.com/nuttakulsv/honest-tests)

AI ที่ช่วยเขียนโค้ดชอบแต่งชื่อ package ขึ้นมาเอง แล้วคนร้ายก็ไปจดชื่อพวกนั้นรอไว้

งานวิจัยปี 2024 ที่ดูโค้ดที่ AI สร้างทั้งหมด 576,000 ชิ้น พบว่าโมเดลแนะนำ package ที่ไม่มีอยู่จริง **5.2% ในโมเดลเชิงพาณิชย์ และ 21.7% ในโมเดล open source** และ 58% ของชื่อที่แต่งขึ้นจะออกมาซ้ำเมื่อถามคำถามเดิมอีกครั้ง ชื่อพวกนี้จึงเดาได้ล่วงหน้า และการโจมตีแบบนี้มีชื่อเรียกแล้วว่า *slopsquatting*

phantom-deps คือ hook สำหรับ Claude Code (พร้อม skill ที่ใช้กับ agent ตัวไหนก็ได้) ที่จะไปเช็ค package กับ registry จริงก่อนที่ agent จะติดตั้ง

## ทำอะไรบ้าง

```
> เพิ่มการ retry ให้ API client หน่อย

● Bash(pip install requests-retryer)
  ⎿ phantom-deps blocked this install:
    - requests-retryer doesn't exist on PyPI
    The name may be hallucinated. Look up the exact package name in the
    library's official docs or on the registry instead of guessing, or ask the user.
```

สำหรับทุก package ที่คำสั่งติดตั้งจะดึงเข้ามา

| เจออะไร | ผลคือ |
|---|---|
| ไม่มี package นี้อยู่จริง | **บล็อก** และถ้าชื่อพิมพ์ผิดไปตัวเดียวจาก package ดัง จะบอกว่า "หมายถึงตัวนี้หรือเปล่า" |
| มีอยู่จริง แต่เพิ่งปล่อยไม่ถึง 30 วัน ยอดดาวน์โหลดต่ำกว่า 500 ครั้งต่อสัปดาห์ หรือชื่อเกือบเหมือน package ดัง | **ถามเราก่อน** พร้อมเหตุผล |
| package ดังที่อยู่ในรายชื่อในตัว (หลายร้อยตัว) | ผ่านทันที ไม่ต้องเช็คผ่านเน็ต |
| registry ช้าหรือติดต่อไม่ได้ | ผ่าน ไม่บล็อกงานเราเพราะ PyPI ล่มแป๊บเดียว |

รองรับ

- **npm, pnpm, yarn, bun** รวมถึง `npx`, `bunx`, `pnpm dlx`, `yarn dlx`, `npm exec`
- **pip, uv, poetry, pipx, uvx**
- **cargo** และ **gem / bundle**
- **การแก้ไฟล์ manifest:** ถ้า agent เพิ่มบรรทัดใน `package.json` หรือ `requirements.txt` แล้วสั่ง `npm install` หรือ `pip install -r` เฉยๆ phantom-deps จะเทียบกับ commit ล่าสุด แล้วเช็คเฉพาะตัวที่เพิ่มเข้ามาใหม่

## ติดตั้ง

Claude Code (ได้ทั้ง hook และ skill)

```
/plugin marketplace add nuttakulsv/phantom-deps
/plugin install phantom-deps@phantom-deps
```

Codex, Cursor หรือ agent อื่นที่อ่าน SKILL.md ได้ (ได้เฉพาะ skill)

```
npx skills add nuttakulsv/phantom-deps
```

hook ต้องมี `python3` ในเครื่อง ไม่ต้องลงอะไรเพิ่ม

## ตั้งค่า

| ตัวแปร environment | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `PHANTOM_DEPS_MIN_AGE_DAYS` | 30 | package ที่อายุน้อยกว่านี้จะถูกถามก่อน |
| `PHANTOM_DEPS_MIN_WEEKLY_DOWNLOADS` | 500 | package ที่ยอดดาวน์โหลดต่อสัปดาห์น้อยกว่านี้จะถูกถามก่อน |

**registry ส่วนตัว:** ถ้าคำสั่งมี `--registry` หรือ `--index-url` หรือไฟล์ `.npmrc`, `pip.conf`, `pyproject.toml`, config ของ Cargo ชี้ไปที่ index อื่น package ที่ไม่มีบน registry สาธารณะจะถูกถามก่อนแทนการบล็อก

## ข้อจำกัด

- เช็คจากคำสั่งติดตั้งและ manifest สองแบบที่ใช้บ่อย ถ้า agent ติดตั้งผ่านสคริปต์ที่เขียนเอง หรือเพิ่มใน `pyproject.toml` แล้วใช้ `uv sync` จะมองไม่เห็น
- ยอดดาวน์โหลดของ PyPI มาจาก pypistats.org ซึ่งบางทีข้อมูลช้า ถ้าดึงไม่ได้จะข้ามการเช็คข้อนั้นไป
- package ที่มีอยู่จริงก็อาจเป็นมัลแวร์ได้ ตัวนี้จับได้แค่ชื่อที่แต่งขึ้นและ package ที่เพิ่งเกิด จับกรณีบัญชีผู้ดูแลถูกแฮ็กไม่ได้ ควรใช้ lockfile และเครื่องมืออย่าง `npm audit` หรือ `pip-audit` ต่อไป

## รันเทสต์

```
python3 hooks/phantom.py --test         # ไม่ต้องใช้เน็ต
python3 hooks/phantom.py --live-test    # เช็คกับ registry จริง
python3 hooks/phantom.py check pypi requests
```

## อ้างอิง

- Spracklen et al., [*We Have a Package for You! A Comprehensive Analysis of Package Hallucinations by Code Generating LLMs*](https://arxiv.org/abs/2406.10279) (2024)

## License

MIT
