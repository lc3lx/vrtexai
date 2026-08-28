# Vertex — استخراج موثّق من الفواتير إلى Excel

تطبيق ويب يقرأ الفواتير المصوّرة والورقية وملفات PDF ويبني منها جداول Excel،
ويُنظّف ملفات Excel المبعثرة. كل قيمة تُقابَل بدليل مستقل في الصورة نفسها، وما لا
يطابق يُعلَّم ولا يُحذف.

Arabic first: the interface is Arabic by default with an English translation,
right-to-left throughout, and every server error carries a code the browser
translates rather than an English sentence the customer cannot read.

## البنية

| المجلّد | ما هو |
|---|---|
| `backend/` | FastAPI + MongoDB (Beanie). يخدم الـAPI **والواجهة** من نفس العملية. |
| `frontend/` | HTML/CSS/JS عادي. لا build step، لا npm. |
| `ocr_worker/` | القارئ: الـOCR والبوابات الثلاث وبناء Excel. يعمل في عملية فرعية. |
| `ai-service/` | خدمة PaddleOCR-VL على GPU (Docker / Modal) — اختيارية. |

الواجهة ملفات ساكنة يخدمها الباك مباشرة: شيء واحد تشغّله، أصل واحد (origin)،
ولا خطوة بناء بين تعديل الملف ورؤية النتيجة.

## التشغيل

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
cp .env.example .env                            # ثم املأ JWT_SECRET
.venv/Scripts/python -m uvicorn app.main:app --reload
```

يتطلّب MongoDB يعمل على `mongodb://localhost:27017` (يُضبط بـ`MONGO_URL`).

| العنوان | ما هو |
|---|---|
| `http://127.0.0.1:8000/` | الصفحة العامة والباقات |
| `http://127.0.0.1:8000/app` | التطبيق: بوابة العميل ولوحة الأدمن |
| `http://127.0.0.1:8000/docs` | توثيق الـAPI |

على قاعدة بيانات فارغة يُنشأ مسؤول واحد وتُطبع كلمة مروره في سجل الخادم **مرة
واحدة**، وتُزرع الباقات الثلاث. التقط كلمة المرور من الطرفية.

## القارئ

`ocr_worker/` هو كود المنتج المكتبي نفسه، لا إعادة تنفيذ له. يشير إليه
`WORKER_ROOT` وقيمته الافتراضية `../ocr_worker`.

متطلباته على لينكس:

```bash
apt install -y tesseract-ocr tesseract-ocr-ara libgl1 libglib2.0-0
backend/.venv/bin/pip install -r ocr_worker/requirements.txt
```

`paddleocr` **غير مطلوب**: يُستورد داخل الدوال ويتراجع القارئ إلى Tesseract عند
غيابه، والنموذج البصري يأتي من `AI_PROVIDER` على أي حال. ثبّته
(`requirements-vl.txt`) فقط إن أردت القراءة المحلية الكاملة — وحينها في بيئة
افتراضية منفصلة، لأن `numpy` مطلوب بإصدارين متعارضين بين الملفين.

> نسخة `ocr_worker` هنا مرآةٌ لـ`ExcelCleaner/ocr_worker` في مستودع المنتج
> المكتبي. عند تعديل أحدهما زامِن الآخر.

## المعالجة

خمس مراحل، تُبلَّغ وهي تجري لا بعد انتهائها:

`رفع الملف` ← `قراءة الشاهد المستقل` ← `النموذج البصري` ← `التحقق من الأدلة
والحساب` ← `بناء Excel`

القارئ يعمل في **عملية فرعية** لأن مكتبات Paddle الأصلية تُسقط خادم الويب إن
حُمّلت داخله. أنابيبه تُقرأ على خيوط لا عبر `asyncio.create_subprocess_exec`،
فذلك يفشل على ويندوز كلما كان الخادم على حلقة `Selector` — وهو ما يعطيه
`uvicorn --reload`.

## الإعدادات

انظر `backend/.env.example`. أهمّها:

| المتغيّر | المعنى |
|---|---|
| `JWT_SECRET` | **مطلوب.** لا قيمة افتراضية عمداً. |
| `MONGO_URL` / `MONGO_DB` | قاعدة البيانات |
| `AI_PROVIDER` | `local` أو `http` (خدمة GPU) أو `openrouter` |
| `WORKER_ROOT` | مسار `ocr_worker` المكتبي |
| `MAX_UPLOAD_MB` / `MAX_PDF_PAGES` | حدود الرفع |

## الاختبارات

```bash
cd backend && python -m unittest discover -s tests
```
