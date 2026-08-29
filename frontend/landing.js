/* Landing page behaviour.
   Reuses i18n.js wholesale — the same T dictionary, the same setLang/setTheme,
   the same data-t contract as the application. The page's own strings are merged
   in rather than kept in a second file, so a phrase never drifts between the
   public page and the product it describes. */

Object.assign(T, {
  ld_skip:["تخطَّ إلى المحتوى","Skip to content"],
  ld_brandsub:["ذكاء اصطناعي للمستندات","Document AI"],
  ld_n_services:["الخدمات","Services"], ld_n_how:["كيف تعمل","How it works"],
  ld_n_pricing:["الباقات","Pricing"],
  ld_signin:["دخول العملاء","Customer sign in"],
  ld_cta:["اطلب اشتراكك","Request access"],

  ld_eyebrow:["شركة رائدة في الذكاء الاصطناعي","A leader in applied AI"],
  ld_h1:["من صورة فاتورة إلى جدول Excel موثّق.",
         "From a photographed invoice to a verified Excel sheet."],
  ld_sub:["ڤيرتكس تقرأ فواتيرك المصوّرة والورقية وملفاتك، وتنظّف جداول Excel المبعثرة، وتسلّمك ملفاً واحداً نظيفاً — كل رقم فيه له دليل في الصورة، وكل قيمة مشكوك فيها تُعلَّم ولا تُحذف.",
          "Vertex reads your photographed, scanned and paper invoices, cleans up scattered Excel sheets, and hands back one clean file — every number backed by evidence in the image, every doubtful value flagged rather than deleted."],
  ld_how_cta:["شاهد كيف تعمل","See how it works"],
  ld_chip1:["ثلاث بوابات تحقق على كل مستند","Three verification gates per document"],
  ld_chip2:["قراءة بالدقة الكاملة دون تصغير","Read at full resolution, never downscaled"],
  ld_chip3:["عربي أولاً — لا ترجمة لاحقة","Arabic first — not a translation layer"],

  ld_t_eyebrow:["المشكلة والحل","The problem and the fix"],
  ld_t_h:["الورق لا يُدخَل يدوياً بعد اليوم.","Manual data entry ends here."],
  ld_t_p:["ساعات من النسخ اليدوي تنتهي برقم خاطئ لا يعرف أحد مصدره. ڤيرتكس تقرأ الصفحة، وتتحقق من كل قيمة مقابل دليل مستقل في الصورة نفسها، ثم تبني الجدول.",
          "Hours of retyping end in one wrong figure nobody can trace. Vertex reads the page, checks every value against independent evidence in the image itself, and only then builds the table."],
  ld_t_before:["قبل","Before"], ld_t_after:["بعد","After"],
  ld_t_b1:["إدخال يدوي بطيء ومكلف","Slow, expensive manual entry"],
  ld_t_b2:["أخطاء لا يمكن تتبّع مصدرها","Errors with no traceable source"],
  ld_t_b3:["جداول مبعثرة وأرقام مخزّنة كنص","Scattered sheets, numbers stored as text"],
  ld_t_a1:["دقائق بدل ساعات","Minutes instead of hours"],
  ld_t_a2:["كل قيمة لها دليل في الصورة","Every value has evidence in the image"],
  ld_t_a3:["ملف Excel واحد نظيف جاهز للتحليل","One clean Excel file, ready to analyse"],

  ld_s_eyebrow:["خدماتنا","What we do"],
  ld_s_h:["ثلاث خدمات، ناتج واحد نظيف.","Three services, one clean output."],
  ld_s1_h:["تنظيف ملفات Excel","Excel file cleaning"],
  ld_s1_p:["صفوف مكرّرة، خلايا مدموجة، أرقام مخزّنة كنص، رؤوس أعمدة متكرّرة عبر الصفحات — نعيدها كلها إلى جدول واحد متّسق جاهز للتحليل والترحيل.",
           "Duplicate rows, merged cells, numbers stored as text, headers repeated across pages — all folded back into one consistent table ready to analyse or import."],
  ld_s2_h:["الفواتير المصوّرة والورقية","Photographed and paper invoices"],
  ld_s2_p:["صوّرها بهاتفك أو امسحها ضوئياً. نقرأ البنود والكميات والأسعار والضريبة والإجماليات ونبنيها جدولاً، بالعربية والإنجليزية معاً وعلى الأختام والخط الصغير.",
           "Photograph or scan them. We read line items, quantities, prices, tax and totals and build the table — Arabic and English, stamps and small print included."],
  ld_s3_h:["الملفات والدفعات","Files and batches"],
  ld_s3_p:["ملفات PDF متعدّدة الصفحات ودفعات كاملة تُعالَج صفحةً صفحة بالدقة نفسها، ويصلك ملف Excel واحد لكل مستند.",
           "Multi-page PDFs and whole batches are processed page by page at the same fidelity, returning one Excel file per document."],

  ld_h_eyebrow:["كيف تعمل","How it works"],
  ld_h_h:["خمس مراحل معلنة، لا صندوق أسود.","Five stages, stated plainly. No black box."],
  ld_h_p:["تتابع المرحلة التي تجري الآن وهي تجري، ويتقدّم الشريط كلما اكتملت مرحلة فعلاً — لا تقديراً مخترعاً لتقدّم النموذج البصري، فهو لا يبلّغ عن تقدّمه.",
          "You follow the stage that is running as it runs, and the bar advances when a stage actually finishes — not on an invented estimate of the vision model's progress, which it never reports."],
  ld_st1_h:["رفع المستند","Upload"],
  ld_st1_p:["صورة أو PDF، حتى 25 ميغابايت، تُقرأ بدقتها الأصلية.","An image or PDF, up to 25 MB, read at its original resolution."],
  ld_st2_h:["قراءة الشاهد المستقل","Independent evidence read"],
  ld_st2_p:["قارئ ثانٍ منفصل يستخرج نصوص الصفحة ليصير مرجعاً للتحقق.","A second, separate reader extracts the page text to serve as the reference."],
  ld_st3_h:["النموذج البصري","Vision model"],
  ld_st3_p:["نموذج بصري يفهم بنية الجدول: البنود والأعمدة والإجماليات.","A vision model reads the table's structure: items, columns and totals."],
  ld_st4_h:["التحقق من الأدلة والحساب","Evidence & arithmetic gates"],
  ld_st4_p:["كل قيمة تُقابَل بالشاهد المستقل، وتُعاد الحسابات؛ ما لم يطابق يُعلَّم.","Every value is matched against the evidence and the arithmetic is recomputed; whatever fails is flagged."],
  ld_st5_h:["بناء ملف Excel","Excel build"],
  ld_st5_p:["ملف واحد، القيم المعلّمة مميّزة بالأصفر ولكل واحدة تعليق يشرح السبب.","One file, flagged values highlighted in yellow, each with a comment explaining why."],

  ld_tr_eyebrow:["لماذا يُوثق بنا","Why teams trust it"],
  ld_tr_h:["الدقة ليست وعداً — إنها إجراء.","Accuracy is a procedure here, not a promise."],
  ld_c1:["بوابات تحقق مستقلة على كل مستند","independent verification gates on every document"],
  ld_c2:["مراحل معلنة تراها وهي تجري","stages you watch as they actually run"],
  ld_c3:["قيمة تُحذف من غير تعليم","values deleted without being flagged"],
  ld_tr1_h:["دليل لكل رقم","Evidence for every number"],
  ld_tr1_p:["لا تُقبل قيمة لمجرّد أن النموذج قالها. تُقابَل بقارئ مستقل وبإعادة حساب، وما لم يطابق يصلك معلَّماً بسببه.",
            "No value is accepted just because the model said it. It is matched against an independent reader and recomputed; whatever fails reaches you flagged, with its reason."],
  ld_tr2_h:["بياناتك معزولة","Your data stays yours"],
  ld_tr2_p:["كل مستند مربوط بحساب واحد، والتحقق من الملكية يتم في الخادم لا في المتصفح. لا يصل أي عميل إلى بيانات عميل آخر.",
            "Every document belongs to one account, and ownership is checked on the server, never in the browser. No customer can reach another's data."],
  ld_tr3_h:["عربي أولاً","Arabic first"],
  ld_tr3_p:["المنتج بُني للمستندات العربية بالخط والاتجاه والأختام والأرقام الضريبية، لا مترجَماً عن واجهة أجنبية.",
            "Built for Arabic documents — script, direction, stamps and tax numbers — rather than translated from a foreign interface."],

  ld_p_eyebrow:["الباقات","Pricing"],
  ld_p_h:["اختر الخطة المناسبة لحجم أعمالك.","Choose the plan that fits your volume."],
  ld_p_p:["كل الباقات تشمل البوابات الثلاث والمعالجة بالدقة الكاملة. الفرق في الحجم الشهري ومستوى الدعم.",
          "Every plan includes all three gates and full-resolution processing. The difference is monthly volume and level of support."],
  ld_p_note:["الأسعار بالدولار الأمريكي. تحتاج حجماً أكبر أو تكاملاً خاصاً؟ اطلب اشتراكك ونرتّب لك خطة مخصّصة.",
             "Prices in US dollars. Need more volume or a custom integration? Request access and we will arrange a tailored plan."],
  ld_p_limit:["حتى {n} صورة شهرياً","Up to {n} images per month"],
  ld_p_pick:["اشترك الآن","Subscribe now"],
  ld_p_badge:["الأكثر طلباً","Most popular"],
  ld_p_monthly:["شهرياً","per month"],
  ld_p_semiannual:["كل 6 أشهر","every 6 months"],
  ld_p_annual:["سنوياً","per year"],
  ld_p_loading:["جارٍ تحميل الباقات…","Loading plans…"],
  ld_p_fail:["تعذّر تحميل الباقات الآن. تواصل معنا وسنرسل لك التفاصيل.",
             "Plans could not be loaded right now. Get in touch and we will send you the details."],

  ld_f_h:["ابدأ بأول مستند اليوم.","Start with your first document today."],
  ld_f_p:["اترك لنا رقم واتساب ونتواصل معك لتفعيل حسابك.",
          "Leave us a WhatsApp number and we will get in touch to activate your account."],
  ld_rights:["© ڤيرتكس. جميع الحقوق محفوظة.","© Vertex. All rights reserved."],

  ld_l_title:["اطلب اشتراكك","Request access"],
  ld_l_sub:["اترك رقم واتساب ونتواصل معك لتفعيل الحساب. لا نرسل رسائل تسويقية.",
            "Leave a WhatsApp number and we will contact you to activate the account. No marketing messages."],
  ld_l_plan:["الباقة المطلوبة","Requested plan"],
  ld_l_name:["الاسم","Your name"],
  ld_l_wa:["رقم الواتساب (مع رمز الدولة)","WhatsApp number (with country code)"],
  ld_l_note:["ملاحظة (اختياري)","Note (optional)"],
  ld_l_send:["أرسل الطلب","Send request"],
  ld_l_sending:["جارٍ الإرسال…","Sending…"],
  ld_l_done_h:["وصلنا طلبك","Your request has arrived"],
  ld_l_done_p:["سنتواصل معك على واتساب قريباً لتفعيل حسابك.",
               "We will reach you on WhatsApp shortly to activate your account."],
  ld_l_badnum:["أدخل رقم واتساب صحيحاً مع رمز الدولة.","Enter a valid WhatsApp number including the country code."],
  ld_l_badname:["اكتب اسمك.","Please enter your name."],
  ld_l_toomany:["وصلتنا طلبات كثيرة من هذا الجهاز. حاول لاحقاً أو راسلنا مباشرة.",
                "Too many requests from this device. Try again later or contact us directly."],
  ld_l_fail:["تعذّر إرسال الطلب. حاول مرة أخرى.","The request could not be sent. Please try again."],
});

const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));

/* ---------------- plans ---------------- */
let PLANS = null;   // null = not loaded yet, [] = loaded and empty
let plansFailed = false;

const PERIOD_KEY = { monthly: "ld_p_monthly", semiannual: "ld_p_semiannual", annual: "ld_p_annual" };
const num = (value) => Number(value || 0).toLocaleString("en-US");

function money(plan) {
  const amount = num(plan.price_amount);
  return plan.currency === "USD" ? "$" + amount : amount + " " + esc(plan.currency);
}

function renderPlans() {
  const grid = document.getElementById("planGrid");
  if (!grid) return;
  if (plansFailed) { grid.innerHTML = `<p class="empty">${esc(t("ld_p_fail"))}</p>`; return; }
  if (PLANS === null) { grid.innerHTML = `<p class="empty">${esc(t("ld_p_loading"))}</p>`; return; }
  if (!PLANS.length) { grid.innerHTML = `<p class="empty">${esc(t("ld_p_fail"))}</p>`; return; }

  const arabic = document.body.dataset.lang === "ar";
  grid.innerHTML = PLANS.map((plan) => {
    const name = (arabic ? plan.name_ar : plan.name_en) || plan.name_ar || plan.name_en;
    const features = (arabic ? plan.features_ar : plan.features_en) || [];
    const limit = t("ld_p_limit").replace("{n}", num(plan.monthly_limit));
    return `<article class="plan${plan.highlighted ? " hl" : ""}" data-badge="${esc(t("ld_p_badge"))}">
      <div class="plan-name">${esc(name)}</div>
      <div class="plan-price"><b>${money(plan)}</b><u>${esc(t(PERIOD_KEY[plan.period] || "ld_p_monthly"))}</u></div>
      <div class="plan-limit">${esc(limit)}</div>
      <ul>${features.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>
      <button class="btn btn-p btn-w" data-plan="${esc(plan.slug)}">${esc(t("ld_p_pick"))}</button>
    </article>`;
  }).join("");

  grid.querySelectorAll("button[data-plan]").forEach((button) => {
    button.addEventListener("click", () => openLead(button.dataset.plan));
  });
}

async function loadPlans() {
  try {
    const response = await fetch("/api/public/plans");
    if (!response.ok) throw new Error(String(response.status));
    PLANS = await response.json();
  } catch (failure) {
    plansFailed = true;
  }
  renderPlans();
}

/* ---------------- subscription request ---------------- */
function closeModal() { document.getElementById("modalHost").innerHTML = ""; }

function openLead(slug) {
  const plan = (PLANS || []).find((entry) => entry.slug === slug);
  const arabic = document.body.dataset.lang === "ar";
  const planName = plan ? (arabic ? plan.name_ar : plan.name_en) || plan.name_ar : "";
  document.getElementById("modalHost").innerHTML = `
    <div class="modal" id="leadModal">
      <div class="card raise"><div class="card-b">
        <form class="leadform" id="leadForm" novalidate>
          <div><h2 style="font-size:18px">${esc(t("ld_l_title"))}</h2>
            <p class="note" style="margin-top:5px">${esc(t("ld_l_sub"))}</p></div>
          <div class="alert a-bad hidden" id="leadError"></div>
          ${planName ? `<div class="rowline"><span>${esc(t("ld_l_plan"))}</span><b>${esc(planName)}</b></div>` : ""}
          <div class="field"><label for="ldName">${esc(t("ld_l_name"))}</label>
            <input id="ldName" name="full_name" autocomplete="name" required></div>
          <div class="field"><label for="ldWa">${esc(t("ld_l_wa"))}</label>
            <input id="ldWa" name="whatsapp" type="tel" inputmode="tel" autocomplete="tel"
                   placeholder="+9627xxxxxxxx" required></div>
          <div class="field"><label for="ldNote">${esc(t("ld_l_note"))}</label>
            <input id="ldNote" name="note" maxlength="500"></div>
          <input class="hp" tabindex="-1" aria-hidden="true" autocomplete="off" name="company">
          <div style="display:flex;gap:10px">
            <button class="btn btn-p" style="flex:1" type="submit" id="ldSend">${esc(t("ld_l_send"))}</button>
            <button class="btn" type="button" id="ldCancel">${esc(t("cancel"))}</button>
          </div>
        </form>
      </div></div>
    </div>`;

  const modal = document.getElementById("leadModal");
  modal.addEventListener("click", (event) => { if (event.target === modal) closeModal(); });
  document.getElementById("ldCancel").addEventListener("click", closeModal);
  document.getElementById("leadForm").addEventListener("submit", (event) => {
    event.preventDefault();
    sendLead(slug);
  });
  document.getElementById("ldName").focus();
}

async function sendLead(slug) {
  const error = document.getElementById("leadError");
  const button = document.getElementById("ldSend");
  const fullName = document.getElementById("ldName").value.trim();
  const whatsapp = document.getElementById("ldWa").value.trim();

  const fail = (key) => {
    error.textContent = t(key);
    error.classList.remove("hidden");
  };
  error.classList.add("hidden");
  if (fullName.length < 2) return fail("ld_l_badname");
  if (!/^\+?[\d\s-]{8,24}$/.test(whatsapp)) return fail("ld_l_badnum");

  button.disabled = true;
  button.textContent = t("ld_l_sending");
  try {
    const response = await fetch("/api/public/leads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        full_name: fullName,
        whatsapp,
        plan_slug: slug || "",
        note: document.getElementById("ldNote").value.trim(),
        company: document.querySelector('#leadForm input[name="company"]').value,
      }),
    });
    if (response.status === 429) throw new Error("ld_l_toomany");
    if (!response.ok) throw new Error("ld_l_fail");
    document.getElementById("modalHost").innerHTML = `
      <div class="modal" id="leadModal">
        <div class="card raise"><div class="card-b" style="display:flex;flex-direction:column;gap:14px">
          <h2 style="font-size:18px">${esc(t("ld_l_done_h"))}</h2>
          <p class="note">${esc(t("ld_l_done_p"))}</p>
          <button class="btn btn-p btn-w" id="ldClose">${esc(t("close"))}</button>
        </div></div>
      </div>`;
    document.getElementById("ldClose").addEventListener("click", closeModal);
  } catch (failure) {
    button.disabled = false;
    button.textContent = t("ld_l_send");
    fail(failure.message === "ld_l_toomany" ? "ld_l_toomany" : "ld_l_fail");
  }
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});

/* ---------------- motion: reveal, counters, nav, tilt ----------------
   All of it opt-out under prefers-reduced-motion; the reveal class is
   neutralised in CSS there, so the page is complete without a single
   observer firing. */
const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function countUp(element) {
  const target = Number(element.dataset.count || 0);
  if (still || target === 0) { element.textContent = String(target); return; }
  const started = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - started) / 900);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = String(Math.round(target * eased));
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function watchReveals() {
  const items = document.querySelectorAll(".reveal");
  if (still || !("IntersectionObserver" in window)) {
    items.forEach((item) => item.classList.add("in"));
    document.querySelectorAll(".count").forEach(countUp);
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry, index) => {
      if (!entry.isIntersecting) return;
      const element = entry.target;
      element.style.transitionDelay = `${Math.min(index, 4) * 70}ms`;
      element.classList.add("in");
      element.querySelectorAll(".count").forEach(countUp);
      observer.unobserve(element);
    });
  }, { rootMargin: "0px 0px -12% 0px", threshold: 0.12 });
  items.forEach((item) => observer.observe(item));
}

function watchNav() {
  const nav = document.getElementById("lnav");
  const mark = () => nav.classList.toggle("stuck", window.scrollY > 60);
  mark();
  window.addEventListener("scroll", mark, { passive: true });
}

function watchTilt() {
  if (still || window.matchMedia("(pointer: coarse)").matches) return;
  document.querySelectorAll(".tilt").forEach((card) => {
    card.addEventListener("pointermove", (event) => {
      const box = card.getBoundingClientRect();
      const x = (event.clientX - box.left) / box.width - 0.5;
      const y = (event.clientY - box.top) / box.height - 0.5;
      card.style.transform =
        `perspective(900px) rotateY(${x * 7}deg) rotateX(${-y * 7}deg) translateZ(6px)`;
    });
    card.addEventListener("pointerleave", () => { card.style.transform = ""; });
  });
}

/* i18n.js calls this after every language switch. */
window.refreshView = renderPlans;

(function start() {
  try { setLang(localStorage.getItem("ec-lang") || "ar"); } catch (e) { setLang("ar"); }
  try {
    const theme = localStorage.getItem("ec-theme");
    if (theme) setTheme(theme);
  } catch (e) {}
  watchNav();
  watchReveals();
  watchTilt();
  loadPlans();
})();
