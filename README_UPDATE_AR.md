# KoveerBridge — تحديث Windows 7 / Python 3.8

تتضمن الحزمة dashboard_3.html، مدقق موارد يبدأ افتراضيًا لمدة 30 دقيقة،
ملفات start/stop/restart، وملف المتطلبات مع psutil.

مهم: لا تحذف dashboard.html أو dashboard_2.html؛ احتفظ بهما كنسخ رجوع.
لا تحذف أي CSV/HST/HAR/telemetry/templates/reports.
الملف Files/active_assets.txt في جذر الريبو بيانات إعدادات ويجب نسخه إلى
MQL4/Files فقط إذا لم يكن الهدف موجودًا، مع إبقاء الأصل.

المدقق لا يقيس GPU. الشبكة وقراءة/كتابة القرص العامة تخص الجهاز كله.
ملخص العمليات يجمع PID فريدًا ويحسب متوسط/أقصى CPU/RAM وفرق Disk I/O بين العينات.
