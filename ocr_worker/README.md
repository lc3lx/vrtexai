# محرك المعالجة المحلي

هذا المجلد هو محرك التطبيق غير المتصل بالإنترنت. تُعالج المستندات محلياً فقط؛ لا توجد مفاتيح API ولا اتصالات سحابية.

## تجهيز مرة واحدة

1. ثبّت Python 3 x64 وTesseract OCR محلياً، مع بيانات اللغة `ara` و`eng`.
2. من هذا المجلد، نفّذ: `py -3 -m pip install -r requirements.txt`
3. إن لم يكن Tesseract ضمن `PATH`، عيّن متغير النظام `TESSERACT_CMD` إلى مسار `tesseract.exe`.

الملفات المدعومة: PDF، Word، Excel، PowerPoint، TIFF/PNG/JPG/BMP/WEBP، CSV، TXT وRTF. يُنشئ التطبيق مجلد `VertexProcessed` يحتوي المخرجات وتقرير الحالة و`review_queue.json` للمراجعة السريعة.
