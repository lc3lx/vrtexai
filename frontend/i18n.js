/* Arabic first, English second. The product is sold into an Arabic market and
   the documents it reads are Arabic; English is the translation, not the base. */
const T = {
  tagline:["استخراج موثّق من الفواتير","Verified invoice extraction"],
  signin:["تسجيل الدخول","Sign in"],
  signinsub:["تُوجَّه إلى واجهتك تلقائياً حسب دورك.","You are routed to your own interface by role."],
  email:["البريد الإلكتروني","Email"], pw:["كلمة المرور","Password"],
  login:["دخول","Sign in"], signout:["خروج","Sign out"],
  signing:["جارٍ الدخول…","Signing in…"],

  nav_dash:["لوحة المعلومات","Dashboard"], nav_up:["رفع مستند","Upload"],
  nav_job:["المعالجة","Processing"], nav_res:["النتيجة","Result"],
  nav_acust:["العملاء","Customers"], nav_ajobs:["كل المهام","All jobs"],
  nav_ausage:["الاستهلاك","Usage"], nav_asys:["حالة النظام","System"],
  nav_aplans:["الباقات","Plans"], nav_aleads:["طلبات الاشتراك","Requests"],
  m_aplans:["الباقات","Plans"], m_aleads:["الطلبات","Requests"],
  m_dash:["اللوحة","Home"], m_up:["رفع","Upload"], m_job:["المعالجة","Job"], m_res:["النتيجة","Result"],
  m_acust:["العملاء","Customers"], m_ajobs:["المهام","Jobs"], m_ausage:["الاستهلاك","Usage"], m_asys:["النظام","System"],
  portal:["بوابة العميل","Customer portal"], console:["لوحة التحكم","Admin console"],
  r_cust:["عميل","CUSTOMER"], r_adm:["مسؤول","ADMIN"],

  s_docs:["مستند هذا الشهر","Documents this month"],
  s_ver:["بنداً مستخرجاً","Items extracted"],
  s_flag:["قيمة تحتاج مراجعة","Values needing review"],
  s_quota:["الحصة الشهرية","Monthly quota"],
  recent:["آخر مهامي","My recent jobs"], newjob:["مستند جديد","New document"],
  scope_c:["تعرض هذه اللوحة مستنداتك وحدك. لا يصل أي عميل إلى بيانات عميل آخر.",
           "This dashboard shows only your documents. No customer can reach another's data."],
  th_file:["الملف","File"], th_status:["الحالة","Status"], th_items:["البنود","Items"],
  th_flag:["مراجعة","Review"], th_time:["الزمن","Time"], th_owner:["العميل","Customer"],
  th_name:["الاسم","Name"], th_mail:["البريد","Email"], th_used:["الاستهلاك","Usage"],
  th_last:["آخر نشاط","Last activity"], th_prov:["المزوّد","Provider"],
  open:["فتح","Open"], track:["تتبّع","Track"], manage:["إدارة","Manage"],
  nojobs:["لا مهام بعد. ارفع أول مستند.","No jobs yet. Upload your first document."],
  nocust:["لا عملاء بعد. أنشئ أول حساب.","No customers yet. Create the first account."],

  st_queued:["في الطابور","Queued"], st_processing:["قيد المعالجة","Processing"],
  st_completed:["مكتمل","Completed"], st_failed:["فشل","Failed"], st_cancelled:["ملغاة","Cancelled"],

  drop:["اسحب صورة أو ملف PDF إلى هنا","Drop an image or PDF here"],
  dropsub:["PNG · JPG · WEBP · TIFF · BMP · PDF — حتى 25 ميغابايت","PNG · JPG · WEBP · TIFF · BMP · PDF — up to 25 MB"],

  /* ---------------- the two kinds of work ----------------
     Priced apart because they cost apart: reading a page calls a vision model,
     tidying a spreadsheet is arithmetic on our own machine. */
  k_extract:["استخراج من مستند","Extract from a document"],
  k_extract_p:["صورة أو PDF لفاتورة — نقرأها ونبني منها جدولاً موثّقاً.",
               "A photo or PDF of an invoice — read it and build a verified table."],
  k_clean:["تنظيف ملف Excel","Clean an Excel file"],
  k_clean_p:["ملف عندك أصلاً — نحذف التكرار ونضبط العناوين والقيم.",
             "A file you already have — duplicates removed, headers and values tidied."],
  k_uses_quota:["يُحتسب من حصتك","Counts against your quota"],
  k_unlimited:["غير محدود","Unlimited"],
  drop_clean:["اسحب ملف Excel أو CSV إلى هنا","Drop an Excel or CSV file here"],
  dropsub_clean:["XLSX · XLSM · XLS · CSV — حتى 25 ميغابايت","XLSX · XLSM · XLS · CSV — up to 25 MB"],
  u_note_clean:["لا يستدعي هذا الخيار أي نموذج ذكاء اصطناعي، ولا يغادر ملفك خادمنا. لذلك هو غير محدود ولا يُحتسب من حصتك الشهرية.",
                "This calls no AI model and your file never leaves our server. That is why it is unlimited and never counted against your monthly quota."],
  g_clean:["تنظيف الجدول","Cleaning the table"],
  th_kind:["النوع","Kind"],
  kind_extract:["استخراج","Extract"], kind_clean:["تنظيف","Clean"],
  r_rows:["الصفوف بعد التنظيف","Rows after cleaning"],
  clean_ok:["اكتمل التنظيف ولم تُصحَّح أي قيمة — الملف كان متّسقاً.",
            "Cleaning finished with no value corrected — the file was already consistent."],
  needs_clean:["صفوف صُحّحت — راجعها","Rows that were corrected — check them"],
  r_clean_note:["حُذفت الصفوف المكرّرة وصفوف العناوين الزائدة، وقُلّمت المسافات ووُحّدت التواريخ والأرقام. الصفوف الصفراء صُحّحت اعتماداً على القوائم المحلية — راجعها.",
                "Duplicate rows and stray header rows were removed, whitespace trimmed, dates and numbers normalised. Yellow rows were corrected against local lists — check them."],
  u_file:["الملف","File"], u_size:["الحجم","Size"], start:["بدء المعالجة","Start processing"],
  uploading:["جارٍ الرفع…","Uploading…"],
  u_note:["تُقرأ الصفحة بدقتها الكاملة دون تصغير — الخط الصغير كالأرقام الضريبية والأختام هو أول ما يضيع عند إعادة التحجيم.",
          "Pages are read at full resolution. Downscaling loses the small print first — tax numbers, stamps, tight table rows."],

  g_upload:["رفع الملف","Upload"], g_evidence_ocr:["قراءة الشاهد المستقل","Independent evidence OCR"],
  g_ai_vision:["قراءة النموذج البصري","Vision model reading"],
  g_verification:["التحقق من الأدلة والحساب","Evidence & arithmetic checks"],
  g_excel:["بناء ملف Excel","Excel build"],
  running:["جارية…","running…"], waiting:["—","—"],
  nojob:["لا مهمة نشطة. ارفع مستنداً لتبدأ.","No active job. Upload a document to begin."],
  nores:["لا نتيجة بعد. افتح مهمة مكتملة.","No result yet. Open a completed job."],

  /* ---------------- live tracking ----------------
     The wording avoids promising a finish time. What it does promise is that
     the line moving on screen is the reader's own account of where it is. */
  j_overall:["التقدّم العام","Overall progress"],
  j_page:["الصفحة {n} من {total}","Page {n} of {total}"],
  j_page1:["صفحة واحدة","One page"],
  j_elapsed:["منذ","for"],
  j_since:["الزمن المنقضي","Elapsed"],
  j_live:["يعمل الآن","Working now"],
  j_queued:["بانتظار دورها","Waiting its turn"],
  j_stagedone:["اكتملت","Done"],
  j_alldone:["اكتملت كل المراحل","Every stage complete"],
  j_failed_at:["توقّفت عند هذه المرحلة","Stopped at this stage"],
  j_slow:["هذه المرحلة تأخذ وقتاً أطول من المعتاد — ما زالت تعمل.",
          "This stage is taking longer than usual — it is still running."],
  j_cancel:["إلغاء المهمة","Cancel job"],
  j_retry:["ارفع مستنداً آخر","Upload another document"],
  j_open_res:["اعرض النتيجة","View the result"],
  j_done_h:["انتهت المعالجة","Processing finished"],
  j_wait_h:["جارٍ العمل على مستندك","Working on your document"],
  j_fail_h:["تعذّرت المعالجة","Processing could not finish"],

  /* Dashboard and upload polish. */
  d_hi:["أهلاً، {name}","Welcome back, {name}"],
  d_sub:["هذه لوحتك. ارفع مستنداً وتابع كل مرحلة وهي تجري.",
         "This is your dashboard. Upload a document and follow each stage as it runs."],
  d_left:["متبقٍ لك {n} مستند هذا الشهر","{n} documents left this month"],
  d_full:["استهلكت حصة هذا الشهر بالكامل","You have used all of this month's quota"],
  u_change:["اختر ملفاً آخر","Choose another file"],
  u_ready:["جاهز للمعالجة","Ready to process"],
  retry:["أعد المحاولة","Try again"],
  loading:["جارٍ التحميل…","Loading…"],

  needs:["قيم بقيت وعُلّمت — لم تُحذف","Kept and flagged — never deleted"],
  clean:["كل القيم لها دليل في الصورة وحساباتها صحيحة.","Every value has evidence in the image and its arithmetic checks out."],
  r_items:["البنود المستخرجة","Items extracted"], r_pages:["الصفحات","Pages"],
  r_prov:["مزوّد النموذج","Model provider"], r_total:["زمن المعالجة","Processing time"],
  dl:["تنزيل ملف Excel","Download Excel"],
  r_note:["القيم المعلّمة مميّزة بالأصفر في الملف، ولكل واحدة تعليق يشرح سبب التعليم. القرار النهائي للمراجع البشري.",
          "Flagged values are highlighted yellow in the file, each with a comment explaining why. The reviewer decides."],

  a_cust:["عميل","Customers"], a_docs:["مهمة هذا الشهر","Jobs this month"],
  a_fail:["مهمة فاشلة","Failed jobs"], a_flag:["قيمة للمراجعة","Flagged values"],
  a_tbl:["العملاء الذين أنشأتهم","Customers you created"], a_new:["عميل جديد","New customer"],
  aj_tbl:["المهام عبر كل عملائك","Jobs across your customers"],
  au_tbl:["الحصص والاستهلاك","Quotas and usage"], as_tbl:["حالة النظام","System status"],
  a_note:["كل عميل مربوط بالمسؤول الذي أنشأه. لا يرى أي عميل بيانات عميل آخر، والتحقق من الملكية يتم في الخادم لا في المتصفح.",
          "Every customer belongs to the admin who created them. No customer sees another's data, and ownership is checked on the server, never in the browser."],
  c_on:["نشط","Active"], c_off:["معطّل","Disabled"], c_lim:["بلغ الحد","At limit"],
  disable:["تعطيل","Disable"], enable:["تفعيل","Enable"], resetpw:["كلمة مرور جديدة","New password"],

  new_title:["عميل جديد","New customer"], f_name:["اسم العميل","Customer name"],
  f_org:["الشركة","Organisation"], f_quota:["الحصة الشهرية","Monthly quota"],
  create:["إنشاء","Create"], cancel:["إلغاء","Cancel"],
  created:["أُنشئ الحساب. سلّم كلمة المرور للعميل — لن تظهر مرة أخرى.",
           "Account created. Hand this password over — it is not shown again."],
  pw_reset:["كلمة المرور الجديدة — لن تظهر مرة أخرى.","The new password — it is not shown again."],
  close:["إغلاق","Close"],

  /* Plans — the price list shown on the public page. */
  ap_tbl:["الباقات المعروضة على الصفحة العامة","Plans shown on the public page"],
  ap_new:["باقة جديدة","New plan"],
  ap_note:["هذه الباقات تظهر للزوّار على الصفحة العامة فور حفظها. تعديل الباقة لا يغيّر حصة أي عميل قائم — الحصة تُضبط على حساب العميل نفسه.",
           "These plans appear to visitors on the public page as soon as they are saved. Editing a plan never changes an existing customer's quota — that is set on the account itself."],
  ap_slug:["المعرّف","Slug"], ap_price:["السعر","Price"], ap_period:["المدة","Billing"],
  ap_limit:["الحد الشهري","Monthly limit"], ap_order:["الترتيب","Order"],
  ap_hl:["مميّزة","Featured"], ap_shown:["معروضة","Shown"], ap_hidden:["مخفية","Hidden"],
  ap_clean:["تنظيف Excel غير محدود","Unlimited Excel cleaning"],
  ap_edit:["تعديل باقة","Edit plan"], ap_add:["باقة جديدة","New plan"],
  ap_name_ar:["الاسم بالعربية","Arabic name"], ap_name_en:["الاسم بالإنجليزية","English name"],
  ap_cur:["العملة","Currency"],
  ap_feat_ar:["المزايا بالعربية (سطر لكل ميزة)","Arabic features (one per line)"],
  ap_feat_en:["المزايا بالإنجليزية (سطر لكل ميزة)","English features (one per line)"],
  ap_hide:["إخفاء","Hide"], ap_show:["إظهار","Show"], save:["حفظ","Save"],
  ap_none:["لا باقات بعد. أنشئ أول باقة.","No plans yet. Create the first one."],
  per_monthly:["شهرياً","Monthly"], per_semiannual:["كل 6 أشهر","Every 6 months"],
  per_annual:["سنوياً","Annual"],

  /* Subscription requests coming off the public page. */
  al_tbl:["طلبات الاشتراك الواردة","Incoming subscription requests"],
  al_note:["كل طلب يصل من الصفحة العامة بالاسم ورقم الواتساب فقط. تواصل مع صاحب الطلب، ثم حوّله إلى حساب عميل من الزر — عندها فقط يُنشأ الحساب وتظهر كلمة المرور مرة واحدة.",
           "Every request arrives from the public page with a name and a WhatsApp number only. Get in touch, then convert it into a customer account — only then is the account created and the password shown once."],
  al_who:["مقدّم الطلب","Requester"], al_wa:["واتساب","WhatsApp"],
  al_plan:["الباقة المطلوبة","Requested plan"], al_when:["التاريخ","Received"],
  al_all:["كل الحالات","All statuses"],
  ls_new:["جديد","New"], ls_contacted:["تمّ التواصل","Contacted"],
  ls_converted:["حُوِّل لعميل","Converted"], ls_rejected:["مرفوض","Rejected"],
  al_contacted:["علّم كمتواصَل معه","Mark contacted"], al_reject:["رفض","Reject"],
  al_convert:["حوّل إلى عميل","Convert to customer"],
  al_convert_h:["إنشاء حساب من الطلب","Create an account from this request"],
  al_convert_p:["البريد يُتّفق عليه في المكالمة، لا من نموذج عام. الحصة الشهرية تُضبط هنا وتبقى مستقلة عن سعر الباقة.",
                "The email is agreed in the call, not taken from a public form. The monthly quota is set here and stays independent of the plan price."],
  al_none:["لا طلبات بعد.","No requests yet."],

  /* ---------------- errors ----------------
     One key per code the server sends. {name} placeholders are filled from the
     params the server attaches, so each language puts the number where its own
     grammar wants it. */
  e_bad_credentials:["البريد الإلكتروني أو كلمة المرور غير صحيحة.","Email or password is incorrect."],
  e_account_locked:["حاولت مرات كثيرة. انتظر {minutes} دقيقة ثم أعد المحاولة.",
                    "Too many attempts. Wait {minutes} minute(s) and try again."],
  e_account_disabled:["هذا الحساب معطَّل. تواصل مع المسؤول لتفعيله.",
                      "This account is disabled. Contact your administrator."],
  e_signin_required:["انتهت الجلسة. سجّل الدخول من جديد.","Your session ended. Please sign in again."],
  e_session_expired:["انتهت صلاحية الجلسة. سجّل الدخول من جديد.","Your session expired. Please sign in again."],
  e_admin_only:["هذا القسم للمسؤولين فقط.","This section is for administrators only."],
  e_customer_only:["الرفع متاح لحسابات العملاء فقط.","Uploading is available to customer accounts only."],
  e_wrong_password:["كلمة المرور الحالية غير صحيحة.","The current password is incorrect."],

  e_quota_exhausted:["استهلكت حصة هذا الشهر ({quota} مستند). اطلب من المسؤول رفعها.",
                     "You have used this month's quota ({quota} documents). Ask your administrator to raise it."],
  e_file_empty:["الملف فارغ. اختر ملفاً آخر.","The file is empty. Choose another one."],
  e_file_too_large:["حجم الملف أكبر من الحد المسموح ({limit} ميغابايت).",
                    "The file is larger than the {limit} MB limit."],
  e_file_type:["نوع الملف غير مدعوم. أرسل PNG أو JPG أو WEBP أو TIFF أو BMP أو PDF.",
               "That file type is not supported. Send a PNG, JPG, WEBP, TIFF, BMP or PDF."],
  e_pdf_unreadable:["تعذّر فتح ملف PDF. قد يكون تالفاً أو محمياً بكلمة مرور.",
                    "This PDF could not be opened. It may be damaged or password-protected."],
  e_not_a_spreadsheet:["هذا ليس ملف جدول. أرسل XLSX أو XLSM أو XLS أو CSV.",
                       "That is not a spreadsheet. Send an XLSX, XLSM, XLS or CSV file."],
  e_clean_failed:["تعذّر تنظيف هذا الملف. تأكّد أنه يحتوي جدولاً بصف عناوين.",
                  "This file could not be cleaned. Check that it holds a table with a header row."],
  e_too_many_pages:["المستند فيه {pages} صفحة، والحد {limit} صفحة. قسّمه إلى ملفات أصغر.",
                    "This document has {pages} pages; the limit is {limit}. Split it into smaller files."],
  e_job_not_found:["هذه المهمة غير موجودة أو لا تخصّ حسابك.","That job does not exist, or is not yours."],
  e_no_workbook:["لا يوجد ملف Excel لهذه المهمة بعد.","There is no workbook for this job yet."],
  e_job_finished:["هذه المهمة انتهت بالفعل.","This job has already finished."],

  e_email_taken:["يوجد حساب بهذا البريد الإلكتروني.","An account with this email already exists."],
  e_customer_not_found:["العميل غير موجود.","That customer was not found."],
  e_plan_not_found:["الباقة غير موجودة.","That plan was not found."],
  e_plan_slug_taken:["يوجد باقة بهذا المعرّف. اختر معرّفاً آخر.",
                     "A plan with this slug already exists. Choose another."],
  e_lead_not_found:["الطلب غير موجود.","That request was not found."],
  e_lead_converted:["هذا الطلب حُوِّل إلى حساب عميل من قبل.","This request was already converted."],
  e_lead_rate_limited:["وصلتنا طلبات كثيرة من هذا الجهاز. حاول لاحقاً.",
                       "Too many requests from this device. Try again later."],

  e_invalid_field:["القيمة المدخلة في «{field}» غير صحيحة. راجعها وحاول مجدداً.",
                   "The value in \"{field}\" is not valid. Check it and try again."],
  e_server_error:["حدث خطأ عندنا وسُجِّل. حاول مرة أخرى بعد قليل.",
                  "Something went wrong on our side and has been logged. Please try again shortly."],
  e_network:["تعذّر الوصول إلى الخادم. تحقّق من اتصالك بالإنترنت.",
             "The server could not be reached. Check your connection."],
  e_unknown:["حدث خطأ غير متوقع.","Something unexpected went wrong."],

  /* Failures recorded on a job by the reader itself. */
  e_reader_failed:["تعذّرت قراءة هذا المستند. جرّب صورة أوضح أو ملفاً آخر.",
                   "This document could not be read. Try a clearer scan or another file."],
  e_reader_crashed:["توقّف القارئ فجأة أثناء المعالجة. أعد المحاولة، وإن تكرّر أبلغ المسؤول.",
                    "The reader stopped unexpectedly. Try again; if it repeats, tell your administrator."],
  e_no_page_read:["لم نتمكّن من قراءة أي صفحة من هذا الملف.","No page in this file could be read."],
  e_source_missing:["لم نعثر على الملف المرفوع. أعد رفعه.","The uploaded file could not be found. Upload it again."],
  e_detail:["التفاصيل التقنية","Technical detail"],

  sys_ai:["خدمة النموذج البصري","Vision model service"],
  sys_prov:["المزوّد الحالي","Active provider"],
  sys_fb:["السقوط المحلي","Local fallback"],
  sys_q:["في الطابور","Queued"], sys_run:["قيد المعالجة","Processing"],
  sys_db:["قاعدة البيانات","Database"],
  sys_up:["متاحة","Online"], sys_down:["غير متاحة","Unavailable"],
  sys_on:["مفعّل","Enabled"], sys_off:["معطّل","Disabled"],
  sys_note:["عند تعذّر خدمة النموذج تتحوّل المعالجة تلقائياً إلى القراءة المحلية، وتبقى البوابات الثلاث فعّالة.",
            "If the model service is unreachable, processing falls back to the local reader and all three gates still run."],
};

/* The interface is English. The Arabic column of every pair above is kept
   deliberately rather than deleted: the product is sold into an Arabic market
   and the translations are done work, so turning Arabic back on is a one-line
   change here instead of a rewrite. Nothing offers the switch to a visitor. */
let LANG = "en";
const idx = () => (LANG === "ar" ? 0 : 1);
const t = (key) => (T[key] ? T[key][idx()] : key);

function setLang(lang) {
  LANG = lang === "ar" ? "ar" : "en";
  lang = LANG;
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
  document.body.dataset.lang = lang;
  document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
  try { localStorage.setItem("ec-lang", lang); } catch (e) {}
  // Messages already on screen follow the toggle too. An error is remembered by
  // its code, so switching language re-says it rather than leaving the previous
  // language stranded in the one place the reader is actually looking.
  if (window.retranslateErrors) window.retranslateErrors();
  if (window.refreshView) window.refreshView();
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  ["1", "2"].forEach((n) => {
    const l = document.getElementById("t-l-" + n), d = document.getElementById("t-d-" + n);
    if (l) l.setAttribute("aria-pressed", String(theme === "light"));
    if (d) d.setAttribute("aria-pressed", String(theme === "dark"));
  });
  try { localStorage.setItem("ec-theme", theme); } catch (e) {}
}
