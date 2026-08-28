/* Excel Clear — browser client.
   Talks only to this backend. It never holds a Hugging Face or Modal
   credential, and never decides what a user may see: every list it renders is
   already scoped by the server to the caller's own tenant. */

const API = "";
let session = null;      // {access, refresh, role, name, email}
let page = "dash";
let currentJob = null;   // id being watched
let lastResult = null;   // completed job detail
let picked = null;       // File chosen for upload
let poll = null;

/* ---------------- session ---------------- */
function loadSession() {
  try { return JSON.parse(localStorage.getItem("ec-session") || "null"); } catch (e) { return null; }
}
function saveSession(value) {
  session = value;
  try {
    if (value) localStorage.setItem("ec-session", JSON.stringify(value));
    else localStorage.removeItem("ec-session");
  } catch (e) {}
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (session && session.access) headers.Authorization = "Bearer " + session.access;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(API + path, { ...options, headers });

  // One silent refresh, then give up and send the user back to sign-in. A
  // retry loop here would hide a genuinely expired session behind a hang.
  if (response.status === 401 && session && session.refresh && !options._retried) {
    const renewed = await fetch(API + "/api/auth/refresh", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: session.refresh }),
    });
    if (renewed.ok) {
      const body = await renewed.json();
      saveSession({ ...session, access: body.access_token, refresh: body.refresh_token });
      return api(path, { ...options, _retried: true });
    }
    signOut();
    throw apiError({ code: "session_expired", message: "session expired", params: {} }, renewed);
  }
  if (!response.ok) {
    let detail = null;
    try { detail = (await response.json()).detail; } catch (e) {}
    throw apiError(detail, response);
  }
  return response.status === 204 ? null : response.json();
}

/* The server sends {code, message, params}; the browser picks the words. The
   English message is kept as the fallback for a code this build has no
   translation for — an untranslated sentence beats a blank alert. */
function apiError(detail, response) {
  const error = new Error(
    (detail && detail.message) || (typeof detail === "string" ? detail : "") ||
    (response ? response.statusText : "") || t("e_network"));
  error.code = (detail && detail.code) || "";
  error.params = (detail && detail.params) || {};
  if (!error.code && response && response.status >= 500) error.code = "server_error";
  return error;
}

/** The sentence to show for a failure, in whichever language is on screen. */
function errText(failure) {
  const key = "e_" + (failure && failure.code);
  let text = failure && failure.code && T[key] ? t(key) : (failure ? failure.message : "");
  const params = (failure && failure.params) || {};
  Object.keys(params).forEach((name) => {
    text = text.split("{" + name + "}").join(params[name]);
  });
  return text || t("e_unknown");
}

/* Errors are remembered by code, not by the words they were shown in, so a
   message already on screen follows the language toggle like everything else. */
function showError(element, failure) {
  element.dataset.ecode = (failure && failure.code) || "";
  element.dataset.emsg = (failure && failure.message) || "";
  element.dataset.eparams = JSON.stringify((failure && failure.params) || {});
  element.textContent = errText(failure);
  element.classList.remove("hidden");
  element.classList.add("shake");
  setTimeout(() => element.classList.remove("shake"), 400);
}

function clearError(element) {
  element.classList.add("hidden");
  delete element.dataset.ecode;
}

window.retranslateErrors = function () {
  document.querySelectorAll("[data-ecode]").forEach((element) => {
    let params = {};
    try { params = JSON.parse(element.dataset.eparams || "{}"); } catch (e) {}
    element.textContent = errText({
      code: element.dataset.ecode, message: element.dataset.emsg, params,
    });
  });
};

/* ---------------- helpers ---------------- */
const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

function ms(value) {
  if (!value) return "—";
  const seconds = value / 1000;
  if (seconds < 60) return seconds.toFixed(1) + "s";
  return Math.floor(seconds / 60) + ":" + String(Math.round(seconds % 60)).padStart(2, "0");
}
function bytes(value) {
  return value >= 1048576 ? (value / 1048576).toFixed(1) + " MB" : Math.round(value / 1024) + " KB";
}
function statusPill(status) {
  const cls = { completed: "p-ok", processing: "p-run", queued: "p-idle",
                failed: "p-bad", cancelled: "p-idle" }[status] || "p-idle";
  return `<span class="pill ${cls}">${esc(t("st_" + status))}</span>`;
}
function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/);
  return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}

/* ---------------- navigation ---------------- */
const CUSTOMER_PAGES = ["dash", "up", "job", "res"];
const ADMIN_PAGES = ["acust", "ajobs", "ausage", "aplans", "aleads", "asys"];

function applyRole() {
  const isAdmin = session && session.role === "admin";
  document.getElementById("v-app").dataset.role = isAdmin ? "admin" : "customer";
  document.getElementById("nav-customer").classList.toggle("hidden", isAdmin);
  document.getElementById("nav-admin").classList.toggle("hidden", !isAdmin);
  document.getElementById("tabs-customer").classList.toggle("hidden", isAdmin);
  document.getElementById("tabs-admin").classList.toggle("hidden", !isAdmin);
  document.getElementById("brandsub").textContent = t(isAdmin ? "console" : "portal");
  document.getElementById("rolechip").textContent = t(isAdmin ? "r_adm" : "r_cust");
  document.getElementById("uname").textContent = session ? session.name : "";
  document.getElementById("urole").textContent = session ? session.email : "";
  document.getElementById("av").textContent = initials(session && session.name);
}

function go(next) {
  // Leaving the tracker stops its poll and its clock. Without this the page
  // keeps calling the server about a job nobody is looking at any more.
  if (page === "job" && next !== "job") stopWatching();
  page = next;
  CUSTOMER_PAGES.concat(ADMIN_PAGES).forEach((name) => {
    const section = document.getElementById("p-" + name);
    if (section) section.classList.toggle("hidden", name !== next);
    ["n-", "m-"].forEach((prefix) => {
      const button = document.getElementById(prefix + name);
      if (button) button.setAttribute("aria-current", String(name === next));
    });
  });
  window.scrollTo(0, 0);
  refreshView();
}

/* The parts of the frame that are words rather than data: the page title, the
   role chip, the portal name. Re-applied on every language switch, because they
   are written once at navigation time and would otherwise be the one corner of
   the screen still speaking the language the reader just left. */
function applyChrome() {
  const titles = { dash: "nav_dash", up: "nav_up", job: "nav_job", res: "nav_res",
                   acust: "nav_acust", ajobs: "nav_ajobs", ausage: "nav_ausage",
                   asys: "nav_asys", aplans: "nav_aplans", aleads: "nav_aleads" };
  document.getElementById("pgtitle").textContent = t(titles[page] || "nav_dash");
  if (session) applyRole();
}

window.refreshView = function () {
  if (!session) return;
  applyChrome();
  const loaders = { dash: loadDashboard, up: renderUpload, job: renderJob, res: renderResult,
                    acust: loadCustomers, ajobs: loadAdminJobs, ausage: loadUsage, asys: loadSystem,
                    aplans: loadPlans, aleads: loadLeads };
  const loader = loaders[page];
  if (loader) loader();
};

/* ---------------- auth ---------------- */
document.getElementById("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("loginBtn");
  const error = document.getElementById("loginError");
  clearError(error);
  button.disabled = true;
  button.textContent = t("signing");
  try {
    const body = await api("/api/auth/login", {
      method: "POST",
      json: { email: document.getElementById("email").value.trim(),
              password: document.getElementById("password").value },
    });
    saveSession({ access: body.access_token, refresh: body.refresh_token,
                  role: body.role, name: body.display_name, email: body.email });
    enterApp();
  } catch (failure) {
    showError(error, failure);
  } finally {
    button.disabled = false;
    button.textContent = t("login");
  }
});

function enterApp() {
  document.getElementById("v-login").classList.add("hidden");
  document.getElementById("v-app").classList.remove("hidden");
  applyRole();
  go(session.role === "admin" ? "acust" : "dash");
}

function signOut() {
  stopWatching();
  saveSession(null);
  currentJob = lastResult = picked = null;
  document.getElementById("v-app").classList.add("hidden");
  document.getElementById("v-login").classList.remove("hidden");
  document.getElementById("password").value = "";
}

/* ---------------- motion ----------------
   Small, and switched off entirely under prefers-reduced-motion by the sheet
   itself. Nothing here carries meaning that the static page does not: it exists
   so a number that changed looks like it changed. */
const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function countUp(element, target) {
  const to = Number(target) || 0;
  if (still || to === 0) { element.textContent = String(to); return; }
  const from = Number(element.dataset.at || 0);
  element.dataset.at = String(to);
  const startedAt = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - startedAt) / 620);
    const eased = 1 - Math.pow(1 - p, 3);
    element.textContent = String(Math.round(from + (to - from) * eased));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/** Stagger children into view once, after a list is (re)built. */
function enter(container) {
  if (still || !container) return;
  [...container.children].forEach((child, index) => {
    child.classList.add("rise");
    child.style.animationDelay = Math.min(index, 6) * 45 + "ms";
    // animationend bubbles: a tick or a spinner finishing inside the row would
    // otherwise cancel the row's own entrance halfway through it.
    const done = (event) => {
      if (event.target !== child) return;
      child.classList.remove("rise");
      child.style.animationDelay = "";
      child.removeEventListener("animationend", done);
    };
    child.addEventListener("animationend", done);
  });
}

/* ---------------- customer ---------------- */
async function loadDashboard() {
  const [profile, jobs] = await Promise.all([api("/api/auth/me"), api("/api/jobs?limit=25")]);
  const flagged = jobs.reduce((sum, job) => sum + (job.flagged || 0), 0);
  const items = jobs.reduce((sum, job) => sum + (job.items || 0), 0);
  const percent = profile.monthly_quota
    ? Math.min(100, Math.round((profile.used_this_month / profile.monthly_quota) * 100)) : 0;
  const left = Math.max(0, profile.monthly_quota - profile.used_this_month);

  const greeting = document.getElementById("custHello");
  greeting.innerHTML = `
    <h2>${esc(t("d_hi").replace("{name}", session.name || ""))}</h2>
    <p class="note">${esc(t("d_sub"))}</p>
    <span class="pill ${left ? "p-ok" : "p-flag"}">${
      esc(left ? t("d_left").replace("{n}", left) : t("d_full"))}</span>`;

  const stats = document.getElementById("custStats");
  stats.innerHTML = `
    <div class="stat"><div class="n" data-count="${jobs.length}">0</div><div class="l">${esc(t("s_docs"))}</div></div>
    <div class="stat"><div class="n" data-count="${items}">0</div><div class="l">${esc(t("s_ver"))}</div></div>
    <div class="stat"><div class="n" data-count="${flagged}">0</div><div class="l">${esc(t("s_flag"))}</div></div>
    <div class="stat"><div class="n"><span data-count="${profile.used_this_month}">0</span><u> / ${profile.monthly_quota}</u></div>
      <div class="l">${esc(t("s_quota"))}</div>
      <div class="meter"><i style="width:0%"></i></div></div>`;
  stats.querySelectorAll("[data-count]").forEach((n) => countUp(n, n.dataset.count));
  // Painted at 0, then widened on the next frame so the fill is a movement the
  // eye can follow rather than a bar that was simply always that long.
  requestAnimationFrame(() => {
    const fill = stats.querySelector(".meter i");
    if (fill) fill.style.width = percent + "%";
  });
  enter(stats);

  document.getElementById("jobsTable").innerHTML = jobs.length ? `
    <table><thead><tr>
      <th>${esc(t("th_file"))}</th><th>${esc(t("th_status"))}</th><th>${esc(t("th_items"))}</th>
      <th>${esc(t("th_flag"))}</th><th>${esc(t("th_time"))}</th><th></th></tr></thead>
    <tbody>${jobs.map((job) => `<tr>
      <td class="mono" data-l="${esc(t("th_file"))}">${esc(job.filename)}</td>
      <td data-l="${esc(t("th_status"))}">${statusPill(job.status)}</td>
      <td class="mono" data-l="${esc(t("th_items"))}">${job.items || "—"}</td>
      <td class="mono" data-l="${esc(t("th_flag"))}">${job.flagged || "—"}</td>
      <td class="mono" data-l="${esc(t("th_time"))}">${ms(job.total_ms)}</td>
      <td class="act"><button class="btn btn-s${
        job.status === "processing" ? " btn-p" : ""}" onclick="openJob('${job.id}')">${
        esc(t(job.status === "completed" ? "open" : "track"))}</button></td>
    </tr>`).join("")}</tbody></table>` : `<div class="empty">${esc(t("nojobs"))}</div>`;
  enter(document.querySelector("#jobsTable tbody"));
}

function renderUpload() {
  const box = document.getElementById("pickedBox");
  box.classList.toggle("hidden", !picked);
  document.getElementById("startBtn").disabled = !picked;
  document.getElementById("drop").classList.toggle("has", !!picked);
  if (picked) {
    document.getElementById("pickedName").textContent = picked.name;
    document.getElementById("pickedSize").textContent = bytes(picked.size);
    if (!still) { box.classList.add("rise"); setTimeout(() => box.classList.remove("rise"), 400); }
  }
}

const drop = document.getElementById("drop");
const fileInput = document.getElementById("fileInput");
fileInput.addEventListener("change", () => { picked = fileInput.files[0] || null; renderUpload(); });
["dragenter", "dragover"].forEach((name) =>
  drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((name) =>
  drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (event) => {
  picked = (event.dataTransfer.files || [])[0] || null;
  renderUpload();
});

document.getElementById("startBtn").addEventListener("click", async () => {
  if (!picked) return;
  const button = document.getElementById("startBtn");
  const error = document.getElementById("upError");
  clearError(error);
  button.disabled = true;
  button.textContent = t("uploading");
  try {
    const form = new FormData();
    form.append("file", picked);
    const job = await api("/api/jobs", { method: "POST", body: form });
    picked = null;
    fileInput.value = "";
    renderUpload();
    openJob(job.id);
  } catch (failure) {
    showError(error, failure);
  } finally {
    button.disabled = false;
    button.textContent = t("start");
  }
});

function openJob(id) {
  currentJob = id;
  go("job");
}

const STAGE_ORDER = ["upload", "evidence_ocr", "ai_vision", "verification", "excel"];
// The three the reader repeats once per page. Everything about progress on a
// multi-page document follows from that.
const PER_PAGE_STAGES = ["evidence_ocr", "ai_vision", "verification"];
const FINAL = ["completed", "failed", "cancelled"];

// The live clocks. `since` is when this reading arrived, so the browser counts
// on from a server measurement rather than comparing two clocks that may
// disagree. `shown` is the last elapsed figure actually displayed: the header
// says "elapsed", and elapsed time is never allowed to go backwards, whatever
// arrives next.
let tick = null;
let live = { stageAt: 0, elapsedAt: 0, since: 0, shown: 0, running: false, job: null };

/** How far along the whole document is — honestly, and never at 100% early. */
function jobProgress(job) {
  if (job.status === "completed") return 1;
  const finished = new Set(job.stages.map((entry) => entry.stage));
  if (job.status === "failed") {
    // Where it stopped, not how much had ever been ticked: a full bar over a
    // failure message is the interface contradicting itself.
    const stopped = STAGE_ORDER.indexOf(job.stage);
    return (stopped >= 0 ? stopped : finished.size) / STAGE_ORDER.length;
  }
  if (job.status !== "processing") return finished.size / STAGE_ORDER.length;

  // upload + (evidence, vision, verification) per page + one workbook build.
  const pages = Math.max(1, job.pages || 1);
  const total = 2 + 3 * pages;
  let units = finished.has("upload") ? 1 : 0;
  units += 3 * Math.min(job.pages_done || 0, pages);
  const within = PER_PAGE_STAGES.indexOf(job.stage);
  if (within > 0) units += within;
  if (job.stage === "excel") units = total - 1;
  return Math.min(0.98, units / total);
}

/** What a single row in the tracker is doing right now. */
function stageState(job, name) {
  const finished = new Set(job.stages.map((entry) => entry.stage));
  if (job.status === "failed") {
    const stopped = STAGE_ORDER.indexOf(job.stage);
    const here = STAGE_ORDER.indexOf(name);
    if (here === stopped) return "bad";
    // A stage past the failure never ran, whatever an earlier page recorded.
    // Showing "done" beneath a failure would claim work that did not happen.
    if (stopped >= 0 && here > stopped) return "todo";
  }
  if (job.status === "processing" && name === job.stage) return "run";
  if (finished.has(name)) return "done";
  if (job.status === "completed") return "done";
  if (job.status === "cancelled") return "todo";
  return "todo";
}

function jobSkeleton(job) {
  const rows = STAGE_ORDER.map((name) => `
    <li class="step" data-stage="${name}">
      <span class="sdot"><b class="check"></b></span>
      <span class="sbody">
        <b class="sname">${esc(t("g_" + name))}</b>
        <small class="sstate"></small>
      </span>
      <span class="sms mono"></span>
    </li>`).join("");

  return `<div class="card raise trackcard">
    <div class="card-h">
      <h2 class="mono" style="word-break:break-all">${esc(job.filename)}</h2>
      <div class="spacer"></div><div id="jkPill"></div>
    </div>
    <div class="card-b">
      <div class="trackhead">
        <div style="min-width:0">
          <b id="jkTitle"></b>
          <small class="note" id="jkSub"></small>
        </div>
        <div class="spacer"></div>
        <div class="clock mono"><span id="jkClock">0.0s</span>
          <small>${esc(t("j_since"))}</small></div>
      </div>
      <div class="trackbar"><i id="jkBar"></i></div>
      <ol class="track" id="jkList">${rows}</ol>
      <div class="alert a-flag hidden" id="jkSlow">${esc(t("j_slow"))}</div>
      <div class="alert a-bad hidden" id="jkErr"></div>
      <details class="detail hidden" id="jkRaw">
        <summary>${esc(t("e_detail"))}</summary><pre class="mono" id="jkRawText"></pre></details>
      <div class="trackact" id="jkAct"></div>
    </div></div>`;
}

function paintJob(job) {
  live.job = job;
  const finished = new Map(job.stages.map((entry) => [entry.stage, entry]));

  document.getElementById("jkPill").innerHTML = statusPill(job.status);

  STAGE_ORDER.forEach((name) => {
    const row = document.querySelector(`#jkList .step[data-stage="${name}"]`);
    const state = stageState(job, name);
    // Only touch the class when it actually changed: rewriting it every poll
    // would restart the tick animation two seconds into every second.
    if (row.dataset.state !== state) {
      row.dataset.state = state;
      row.className = "step " + state;
    }
    const entry = finished.get(name);
    const label = { done: "j_stagedone", run: "j_live", bad: "j_failed_at", todo: "j_queued" }[state];
    row.querySelector(".sstate").textContent = t(label);
    row.querySelector(".sname").textContent = t("g_" + name);
    if (state !== "run") {
      // A timing belongs to work that happened. A stage shown as waiting must
      // not carry a duration from a page that ran before the failure.
      row.querySelector(".sms").textContent =
        state === "done" && entry && entry.ms ? ms(entry.ms) : "—";
    }
  });

  const bar = document.getElementById("jkBar");
  bar.style.width = Math.round(jobProgress(job) * 100) + "%";
  bar.classList.toggle("bad", job.status === "failed");

  const pages = Math.max(1, job.pages || 1);
  const title = { processing: "j_wait_h", completed: "j_done_h", failed: "j_fail_h" }[job.status];
  document.getElementById("jkTitle").textContent = t(title || "j_wait_h");
  document.getElementById("jkSub").textContent = job.status === "processing"
    ? (pages > 1
        ? t("j_page").replace("{n}", Math.min(pages, (job.pages_done || 0) + 1)).replace("{total}", pages)
        : t("j_page1"))
    : job.status === "completed" ? t("j_alldone") : "";

  const errorBox = document.getElementById("jkErr");
  if (job.status === "failed") {
    showError(errorBox, { code: job.error_code, message: job.error, params: {} });
    const raw = document.getElementById("jkRaw");
    // The reader's own words, folded away: useless to the customer, and the
    // only thing worth having when they forward the screen to support.
    raw.classList.toggle("hidden", !job.error);
    document.getElementById("jkRawText").textContent = job.error || "";
  } else {
    clearError(errorBox);
    document.getElementById("jkRaw").classList.add("hidden");
  }

  const actions = document.getElementById("jkAct");
  actions.innerHTML = job.status === "completed"
    ? `<button class="btn btn-p btn-w" onclick="showResult('${job.id}')">${esc(t("j_open_res"))}</button>`
    : job.status === "processing"
      ? `<button class="btn btn-w" onclick="cancelJob('${job.id}')">${esc(t("j_cancel"))}</button>`
      : `<button class="btn btn-w" onclick="go('up')">${esc(t("j_retry"))}</button>`;

  live.running = job.status === "processing";
  live.stageAt = job.stage_ms || 0;
  live.elapsedAt = job.elapsed_ms || 0;
  live.since = performance.now();
  if (!live.running) {
    live.shown = 0;
    document.getElementById("jkClock").textContent = ms(job.elapsed_ms || job.total_ms);
    document.getElementById("jkSlow").classList.add("hidden");
  }
}

/** Runs between polls so the elapsed figure never sits still while work does. */
function startTicking() {
  if (tick) clearInterval(tick);
  tick = setInterval(() => {
    if (!live.running) return;
    const clock = document.getElementById("jkClock");
    if (!clock) { clearInterval(tick); tick = null; return; }
    const ahead = performance.now() - live.since;
    const stageMs = live.stageAt + ahead;
    live.shown = Math.max(live.shown, live.elapsedAt + ahead);
    clock.textContent = ms(live.shown);
    const row = document.querySelector("#jkList .step.run .sms");
    if (row) row.textContent = ms(stageMs);
    // Ninety seconds on one stage is unusual but not wrong. Saying so is the
    // difference between a long wait and a wait that feels like a fault.
    const slow = document.getElementById("jkSlow");
    if (slow) slow.classList.toggle("hidden", stageMs < 90000);
  }, 200);
}

function stopWatching() {
  if (poll) { clearInterval(poll); poll = null; }
  if (tick) { clearInterval(tick); tick = null; }
  live.running = false;
}

async function renderJob() {
  stopWatching();
  const host = document.getElementById("jobView");
  if (!currentJob) {
    host.innerHTML = `<div class="card"><div class="empty">${esc(t("nojob"))}</div></div>`;
    return;
  }

  let built = false;
  const draw = async () => {
    let job;
    try {
      job = await api("/api/jobs/" + currentJob);
    } catch (failure) {
      stopWatching();
      host.innerHTML = `<div class="card"><div class="card-b">
        <div class="alert a-bad" id="jobLoadErr"></div>
        <button class="btn btn-w" style="margin-top:12px" onclick="renderJob()">${
          esc(t("retry"))}</button></div></div>`;
      showError(document.getElementById("jobLoadErr"), failure);
      return;
    }
    // Built once, then patched. Replacing the markup on every poll would restart
    // every animation and make a steady process look like a stuttering one.
    if (!built) { host.innerHTML = jobSkeleton(job); built = true; enter(document.getElementById("jkList")); }
    paintJob(job);

    if (job.status === "completed") lastResult = job;
    if (FINAL.includes(job.status)) stopWatching();
  };

  await draw();
  startTicking();
  // A second and a half: fast enough that a stage change is noticed almost at
  // once, slow enough that a reading taking minutes is not thousands of calls.
  poll = setInterval(draw, 1500);
}

async function cancelJob(id) {
  try {
    await api("/api/jobs/" + id + "/cancel", { method: "POST" });
  } catch (failure) {
    const box = document.getElementById("jkErr");
    if (box) showError(box, failure);
    return;
  }
  renderJob();
}

async function showResult(id) {
  lastResult = await api("/api/jobs/" + (id || currentJob));
  go("res");
}

async function renderResult() {
  const host = document.getElementById("resView");
  if (!lastResult) { host.innerHTML = `<div class="card"><div class="empty">${esc(t("nores"))}</div></div>`; return; }
  let job;
  try {
    job = await api("/api/jobs/" + lastResult.id);
  } catch (failure) {
    host.innerHTML = `<div class="card"><div class="card-b">
      <div class="alert a-bad" id="resErr"></div>
      <button class="btn btn-w" style="margin-top:12px" onclick="renderResult()">${
        esc(t("retry"))}</button></div></div>`;
    showError(document.getElementById("resErr"), failure);
    return;
  }
  lastResult = job;

  const flags = job.flags && job.flags.length
    ? `<div style="display:flex;flex-direction:column;gap:9px">
         <div class="eyebrow">${esc(t("needs"))}</div>
         ${job.flags.map((flag) => `<div class="flagrow"><div>
            <div class="mono">${esc(flag.cell)} · ${esc(flag.value ?? "")}</div>
            <p>${esc(flag.reason)}</p></div></div>`).join("")}
       </div>`
    : `<div class="alert a-ok">${esc(t("clean"))}</div>`;

  host.innerHTML = `<div class="card">
    <div class="card-h"><h2 class="mono" style="word-break:break-all">${esc(job.filename)}</h2>
      <div class="spacer"></div>${statusPill(job.status)}</div>
    <div class="card-b" style="display:flex;flex-direction:column;gap:16px">
      ${flags}
      <div>
        <div class="rowline"><span>${esc(t("r_items"))}</span><span class="mono">${job.items}</span></div>
        <div class="rowline"><span>${esc(t("r_pages"))}</span><span class="mono">${job.pages}</span></div>
        <div class="rowline"><span>${esc(t("r_prov"))}</span><span class="mono">${esc(job.provider || "—")}</span></div>
        <div class="rowline"><span>${esc(t("r_total"))}</span><span class="mono">${ms(job.total_ms)}</span></div>
      </div>
      <div class="alert a-bad hidden" id="dlErr"></div>
      ${job.has_result
        ? `<button class="btn btn-p btn-w" onclick="downloadResult('${job.id}')">${esc(t("dl"))}</button>` : ""}
      <p class="note">${esc(t("r_note"))}</p>
    </div></div>`;
  enter(host.querySelector(".card-b"));
}

async function downloadResult(id) {
  const error = document.getElementById("dlErr");
  clearError(error);
  // Fetched with the session header, then handed to the browser as a blob: a
  // plain link would carry no credentials and come back 401.
  let response;
  try {
    response = await fetch(API + "/api/jobs/" + id + "/result",
      { headers: { Authorization: "Bearer " + session.access } });
  } catch (failure) {
    showError(error, { code: "network", message: "" });
    return;
  }
  if (!response.ok) {
    let detail = null;
    try { detail = (await response.json()).detail; } catch (e) {}
    showError(error, apiError(detail, response));
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = (lastResult ? lastResult.filename.replace(/\.[^.]+$/, "") : "result") + ".xlsx";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/* ---------------- admin ---------------- */
async function loadCustomers() {
  const [customers, jobs] = await Promise.all([
    api("/api/admin/customers"), api("/api/admin/jobs?limit=200"),
  ]);
  const failed = jobs.filter((job) => job.status === "failed").length;
  const flagged = jobs.reduce((sum, job) => sum + (job.flagged || 0), 0);
  document.getElementById("admStats").innerHTML = `
    <div class="stat"><div class="n">${customers.length}</div><div class="l">${esc(t("a_cust"))}</div></div>
    <div class="stat"><div class="n">${jobs.length}</div><div class="l">${esc(t("a_docs"))}</div></div>
    <div class="stat"><div class="n">${failed}</div><div class="l">${esc(t("a_fail"))}</div></div>
    <div class="stat"><div class="n">${flagged}</div><div class="l">${esc(t("a_flag"))}</div></div>`;

  document.getElementById("custTable").innerHTML = customers.length ? `
    <table><thead><tr>
      <th>${esc(t("th_name"))}</th><th>${esc(t("th_mail"))}</th><th>${esc(t("th_status"))}</th>
      <th>${esc(t("th_used"))}</th><th></th></tr></thead>
    <tbody>${customers.map((customer) => {
      const atLimit = customer.monthly_quota && customer.used_this_month >= customer.monthly_quota;
      const pill = !customer.active ? `<span class="pill p-idle">${esc(t("c_off"))}</span>`
        : atLimit ? `<span class="pill p-flag">${esc(t("c_lim"))}</span>`
        : `<span class="pill p-ok">${esc(t("c_on"))}</span>`;
      return `<tr>
        <td data-l="${esc(t("th_name"))}">${esc(customer.display_name)}</td>
        <td class="mono" data-l="${esc(t("th_mail"))}" style="word-break:break-all">${esc(customer.email)}</td>
        <td data-l="${esc(t("th_status"))}">${pill}</td>
        <td class="mono" data-l="${esc(t("th_used"))}">${customer.used_this_month} / ${customer.monthly_quota}</td>
        <td class="act"><button class="btn btn-s" onclick="manage('${customer.id}',${customer.active})">${
          esc(t("manage"))}</button></td></tr>`;
    }).join("")}</tbody></table>` : `<div class="empty">${esc(t("nocust"))}</div>`;
}

function modal(inner) {
  document.getElementById("modalHost").innerHTML =
    `<div class="modal" onclick="if(event.target===this)closeModal()"><div class="card raise">${inner}</div></div>`;
}
function closeModal() { document.getElementById("modalHost").innerHTML = ""; }

function openNewCustomer() {
  modal(`<div class="card-b" style="display:flex;flex-direction:column;gap:14px">
    <h2 style="font-size:17px">${esc(t("new_title"))}</h2>
    <div class="alert a-bad hidden" id="newErr"></div>
    <div class="field"><label>${esc(t("th_mail"))}</label><input type="email" id="nEmail"></div>
    <div class="field"><label>${esc(t("f_name"))}</label><input id="nName"></div>
    <div class="field"><label>${esc(t("f_org"))}</label><input id="nOrg"></div>
    <div class="field"><label>${esc(t("f_quota"))}</label><input type="number" id="nQuota" value="500" min="0"></div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-p" style="flex:1" onclick="createCustomer()">${esc(t("create"))}</button>
      <button class="btn" style="flex:1" onclick="closeModal()">${esc(t("cancel"))}</button>
    </div></div>`);
}

async function createCustomer() {
  const error = document.getElementById("newErr");
  clearError(error);
  try {
    const created = await api("/api/admin/customers", { method: "POST", json: {
      email: document.getElementById("nEmail").value.trim(),
      display_name: document.getElementById("nName").value.trim(),
      organisation: document.getElementById("nOrg").value.trim(),
      monthly_quota: parseInt(document.getElementById("nQuota").value, 10) || 0,
    }});
    showPassword(created.email, created.password, t("created"));
    loadCustomers();
  } catch (failure) {
    showError(error, failure);
  }
}

function showPassword(email, password, message) {
  modal(`<div class="card-b" style="display:flex;flex-direction:column;gap:14px">
    <div class="alert a-ok">${esc(message)}</div>
    <div class="rowline"><span>${esc(t("th_mail"))}</span><span class="mono">${esc(email)}</span></div>
    <div class="rowline"><span>${esc(t("pw"))}</span><span class="mono" style="font-size:15px;font-weight:600">${esc(password)}</span></div>
    <button class="btn btn-p btn-w" onclick="closeModal()">${esc(t("close"))}</button></div>`);
}

function manage(id, active) {
  modal(`<div class="card-b" style="display:flex;flex-direction:column;gap:10px">
    <h2 style="font-size:17px">${esc(t("manage"))}</h2>
    <button class="btn btn-w" onclick="toggleCustomer('${id}',${!active})">${
      esc(t(active ? "disable" : "enable"))}</button>
    <button class="btn btn-w" onclick="resetCustomerPassword('${id}')">${esc(t("resetpw"))}</button>
    <button class="btn btn-w" onclick="closeModal()">${esc(t("cancel"))}</button></div>`);
}

async function toggleCustomer(id, active) {
  await api("/api/admin/customers/" + id, { method: "PATCH", json: { active } });
  closeModal();
  loadCustomers();
}
async function resetCustomerPassword(id) {
  const body = await api("/api/admin/customers/" + id + "/password", { method: "POST" });
  showPassword("—", body.password, t("pw_reset"));
}

async function loadAdminJobs() {
  const jobs = await api("/api/admin/jobs?limit=200");
  document.getElementById("admJobsTable").innerHTML = jobs.length ? `
    <table><thead><tr>
      <th>${esc(t("th_file"))}</th><th>${esc(t("th_owner"))}</th><th>${esc(t("th_status"))}</th>
      <th>${esc(t("th_prov"))}</th><th>${esc(t("th_time"))}</th></tr></thead>
    <tbody>${jobs.map((job) => `<tr>
      <td class="mono" data-l="${esc(t("th_file"))}">${esc(job.filename)}</td>
      <td data-l="${esc(t("th_owner"))}">${esc(job.customer)}</td>
      <td data-l="${esc(t("th_status"))}">${statusPill(job.status)}</td>
      <td class="mono" data-l="${esc(t("th_prov"))}">${esc(job.ai_provider || "—")}</td>
      <td class="mono" data-l="${esc(t("th_time"))}">${ms(job.total_ms)}</td>
    </tr>`).join("")}</tbody></table>` : `<div class="empty">${esc(t("nojobs"))}</div>`;
}

async function loadUsage() {
  const rows = await api("/api/admin/usage");
  document.getElementById("usageBox").innerHTML = rows.length
    ? rows.map((row) => {
        const percent = row.quota ? Math.min(100, Math.round((row.used / row.quota) * 100)) : 0;
        const colour = percent >= 100 ? "background:var(--flag)" : "";
        return `<div style="margin-bottom:14px">
          <div class="rowline"><span>${esc(row.name)}</span>
            <span class="mono">${row.used} / ${row.quota}</span></div>
          <div class="meter"><i style="width:${percent}%;${colour}"></i></div></div>`;
      }).join("")
    : `<div class="empty">${esc(t("nocust"))}</div>`;
}

async function loadSystem() {
  const info = await api("/api/admin/system");
  document.getElementById("sysBox").innerHTML = `
    <div class="rowline"><span>${esc(t("sys_ai"))}</span>
      <span class="pill ${info.ai_ready ? "p-ok" : "p-bad"}">${
        esc(t(info.ai_ready ? "sys_up" : "sys_down"))}</span></div>
    <div class="rowline"><span>${esc(t("sys_prov"))}</span><span class="mono">${esc(info.ai_provider)}</span></div>
    <div class="rowline"><span>${esc(t("sys_fb"))}</span>
      <span class="pill ${info.fallback_local ? "p-ok" : "p-idle"}">${
        esc(t(info.fallback_local ? "sys_on" : "sys_off"))}</span></div>
    <div class="rowline"><span>${esc(t("sys_q"))}</span><span class="mono">${info.queued}</span></div>
    <div class="rowline"><span>${esc(t("sys_run"))}</span><span class="mono">${info.processing}</span></div>
    <div class="rowline"><span>${esc(t("sys_db"))}</span><span class="mono">${esc(info.database)}</span></div>
    <p class="note" style="margin-top:12px">${esc(info.ai_detail || "")}</p>
    <p class="note">${esc(t("sys_note"))}</p>`;
}

/* ---------------- plans ----------------
   The price list behind the public page. Editing one changes what a visitor is
   quoted and nothing else: a customer's monthly quota lives on their account,
   so a price change can never throttle someone mid-month. */
const PERIODS = ["monthly", "semiannual", "annual"];
let plans = [];

function planName(plan) {
  return (LANG === "ar" ? plan.name_ar : plan.name_en) || plan.name_ar || plan.name_en;
}

async function loadPlans() {
  plans = await api("/api/admin/plans");
  document.getElementById("planTable").innerHTML = plans.length ? `
    <table><thead><tr>
      <th>${esc(t("th_name"))}</th><th>${esc(t("ap_price"))}</th><th>${esc(t("ap_limit"))}</th>
      <th>${esc(t("th_status"))}</th><th></th></tr></thead>
    <tbody>${plans.map((plan) => `<tr>
      <td data-l="${esc(t("th_name"))}">${esc(planName(plan))}${
        plan.highlighted ? ` <span class="pill p-flag">${esc(t("ap_hl"))}</span>` : ""}</td>
      <td class="mono" data-l="${esc(t("ap_price"))}">${plan.price_amount} ${esc(plan.currency)}
        <u style="text-decoration:none;color:var(--muted)"> · ${esc(t("per_" + plan.period))}</u></td>
      <td class="mono" data-l="${esc(t("ap_limit"))}">${plan.monthly_limit.toLocaleString("en-US")}</td>
      <td data-l="${esc(t("th_status"))}"><span class="pill ${plan.active ? "p-ok" : "p-idle"}">${
        esc(t(plan.active ? "ap_shown" : "ap_hidden"))}</span></td>
      <td class="act"><button class="btn btn-s" onclick="openPlan('${plan.id}')">${
        esc(t("manage"))}</button></td>
    </tr>`).join("")}</tbody></table>` : `<div class="empty">${esc(t("ap_none"))}</div>`;
}

function openPlan(id) {
  const plan = plans.find((entry) => entry.id === id) || {
    slug: "", name_ar: "", name_en: "", price_amount: 0, currency: "USD", period: "monthly",
    monthly_limit: 0, features_ar: [], features_en: [], highlighted: false,
    sort_order: plans.length + 1, active: true,
  };
  const lines = (list) => esc((list || []).join("\n"));
  modal(`<div class="card-b" style="display:flex;flex-direction:column;gap:13px;max-height:82vh;overflow:auto">
    <h2 style="font-size:17px">${esc(t(id ? "ap_edit" : "ap_add"))}</h2>
    <div class="alert a-bad hidden" id="planErr"></div>
    <div class="field"><label>${esc(t("ap_slug"))}</label>
      <input id="pSlug" class="mono" value="${esc(plan.slug)}" ${id ? "disabled" : ""}
             placeholder="silver"></div>
    <div class="field"><label>${esc(t("ap_name_ar"))}</label><input id="pNameAr" value="${esc(plan.name_ar)}"></div>
    <div class="field"><label>${esc(t("ap_name_en"))}</label><input id="pNameEn" value="${esc(plan.name_en)}"></div>
    <div style="display:flex;gap:10px">
      <div class="field" style="flex:1"><label>${esc(t("ap_price"))}</label>
        <input type="number" id="pPrice" min="0" value="${plan.price_amount}"></div>
      <div class="field" style="width:90px"><label>${esc(t("ap_cur"))}</label>
        <input id="pCur" value="${esc(plan.currency)}"></div>
    </div>
    <div class="field"><label>${esc(t("ap_period"))}</label>
      <select id="pPeriod">${PERIODS.map((period) =>
        `<option value="${period}"${plan.period === period ? " selected" : ""}>${
          esc(t("per_" + period))}</option>`).join("")}</select></div>
    <div style="display:flex;gap:10px">
      <div class="field" style="flex:1"><label>${esc(t("ap_limit"))}</label>
        <input type="number" id="pLimit" min="0" value="${plan.monthly_limit}"></div>
      <div class="field" style="width:90px"><label>${esc(t("ap_order"))}</label>
        <input type="number" id="pOrder" min="0" value="${plan.sort_order}"></div>
    </div>
    <div class="field"><label>${esc(t("ap_feat_ar"))}</label>
      <textarea id="pFeatAr" rows="4">${lines(plan.features_ar)}</textarea></div>
    <div class="field"><label>${esc(t("ap_feat_en"))}</label>
      <textarea id="pFeatEn" rows="4">${lines(plan.features_en)}</textarea></div>
    <label class="rowline" style="cursor:pointer"><span>${esc(t("ap_hl"))}</span>
      <input type="checkbox" id="pHl" ${plan.highlighted ? "checked" : ""}
             style="width:18px;height:18px"></label>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn btn-p" style="flex:1" onclick="savePlan(${id ? `'${id}'` : "null"})">${
        esc(t("save"))}</button>
      ${id ? `<button class="btn" onclick="togglePlan('${id}',${!plan.active})">${
        esc(t(plan.active ? "ap_hide" : "ap_show"))}</button>` : ""}
      <button class="btn" onclick="closeModal()">${esc(t("cancel"))}</button>
    </div></div>`);
}

const splitLines = (id) => document.getElementById(id).value
  .split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 12);

async function savePlan(id) {
  const error = document.getElementById("planErr");
  clearError(error);
  const body = {
    name_ar: document.getElementById("pNameAr").value.trim(),
    name_en: document.getElementById("pNameEn").value.trim(),
    price_amount: parseInt(document.getElementById("pPrice").value, 10) || 0,
    currency: document.getElementById("pCur").value.trim().toUpperCase() || "USD",
    period: document.getElementById("pPeriod").value,
    monthly_limit: parseInt(document.getElementById("pLimit").value, 10) || 0,
    sort_order: parseInt(document.getElementById("pOrder").value, 10) || 0,
    features_ar: splitLines("pFeatAr"),
    features_en: splitLines("pFeatEn"),
    highlighted: document.getElementById("pHl").checked,
  };
  try {
    if (id) await api("/api/admin/plans/" + id, { method: "PATCH", json: body });
    else await api("/api/admin/plans", { method: "POST",
      json: { ...body, slug: document.getElementById("pSlug").value.trim().toLowerCase() } });
    closeModal();
    loadPlans();
  } catch (failure) {
    showError(error, failure);
  }
}

async function togglePlan(id, active) {
  if (active) await api("/api/admin/plans/" + id, { method: "PATCH", json: { active: true } });
  else await api("/api/admin/plans/" + id, { method: "DELETE" });
  closeModal();
  loadPlans();
}

/* ---------------- subscription requests ----------------
   These arrive from the public page with a name and a WhatsApp number, nothing
   more. Nobody gets an account by filling in a form: the admin talks to them
   first, and conversion is the deliberate second step. */
const LEAD_STATES = ["new", "contacted", "converted", "rejected"];
let leads = [];

async function loadLeads() {
  const filter = document.getElementById("leadFilter");
  if (!filter.options.length) {
    filter.innerHTML = `<option value="">${esc(t("al_all"))}</option>` +
      LEAD_STATES.map((state) => `<option value="${state}">${esc(t("ls_" + state))}</option>`).join("");
  }
  const chosen = filter.value;
  leads = await api("/api/admin/leads" + (chosen ? "?status_filter=" + chosen : ""));

  const pill = { new: "p-run", contacted: "p-flag", converted: "p-ok", rejected: "p-idle" };
  document.getElementById("leadTable").innerHTML = leads.length ? `
    <table><thead><tr>
      <th>${esc(t("al_who"))}</th><th>${esc(t("al_wa"))}</th><th>${esc(t("al_plan"))}</th>
      <th>${esc(t("th_status"))}</th><th>${esc(t("al_when"))}</th><th></th></tr></thead>
    <tbody>${leads.map((lead) => `<tr>
      <td data-l="${esc(t("al_who"))}">${esc(lead.full_name)}${
        lead.note ? `<small style="display:block;color:var(--muted)">${esc(lead.note)}</small>` : ""}</td>
      <td data-l="${esc(t("al_wa"))}"><a class="mono" dir="ltr" style="color:var(--accent)"
        href="https://wa.me/${encodeURIComponent(lead.whatsapp.replace(/\D/g, ""))}"
        target="_blank" rel="noopener">${esc(lead.whatsapp)}</a></td>
      <td data-l="${esc(t("al_plan"))}">${esc(lead.plan_name || "—")}</td>
      <td data-l="${esc(t("th_status"))}"><span class="pill ${pill[lead.status] || "p-idle"}">${
        esc(t("ls_" + lead.status))}</span></td>
      <td class="mono" data-l="${esc(t("al_when"))}">${new Date(lead.created_at).toLocaleDateString("en-GB")}</td>
      <td class="act"><button class="btn btn-s" onclick="manageLead('${lead.id}')">${
        esc(t("manage"))}</button></td>
    </tr>`).join("")}</tbody></table>` : `<div class="empty">${esc(t("al_none"))}</div>`;
}

function manageLead(id) {
  const lead = leads.find((entry) => entry.id === id);
  const done = lead.status === "converted";
  modal(`<div class="card-b" style="display:flex;flex-direction:column;gap:10px">
    <h2 style="font-size:17px">${esc(lead.full_name)}</h2>
    <div class="rowline"><span>${esc(t("al_wa"))}</span><span class="mono" dir="ltr">${esc(lead.whatsapp)}</span></div>
    <div class="rowline"><span>${esc(t("al_plan"))}</span><span>${esc(lead.plan_name || "—")}</span></div>
    ${done ? "" : `
    <button class="btn btn-w btn-p" onclick="openConvert('${id}')">${esc(t("al_convert"))}</button>
    <button class="btn btn-w" onclick="setLeadStatus('${id}','contacted')">${esc(t("al_contacted"))}</button>
    <button class="btn btn-w" onclick="setLeadStatus('${id}','rejected')">${esc(t("al_reject"))}</button>`}
    <button class="btn btn-w" onclick="closeModal()">${esc(t("close"))}</button></div>`);
}

async function setLeadStatus(id, status) {
  await api("/api/admin/leads/" + id, { method: "PATCH", json: { status } });
  closeModal();
  loadLeads();
}

function openConvert(id) {
  const lead = leads.find((entry) => entry.id === id);
  modal(`<div class="card-b" style="display:flex;flex-direction:column;gap:13px">
    <div><h2 style="font-size:17px">${esc(t("al_convert_h"))}</h2>
      <p class="note" style="margin-top:5px">${esc(t("al_convert_p"))}</p></div>
    <div class="alert a-bad hidden" id="cvErr"></div>
    <div class="field"><label>${esc(t("th_mail"))}</label><input type="email" id="cvEmail"></div>
    <div class="field"><label>${esc(t("f_name"))}</label><input id="cvName" value="${esc(lead.full_name)}"></div>
    <div class="field"><label>${esc(t("f_org"))}</label><input id="cvOrg"></div>
    <div class="field"><label>${esc(t("f_quota"))}</label>
      <input type="number" id="cvQuota" min="0" value="500"></div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-p" style="flex:1" onclick="convertLead('${id}')">${esc(t("create"))}</button>
      <button class="btn" style="flex:1" onclick="closeModal()">${esc(t("cancel"))}</button>
    </div></div>`);
}

async function convertLead(id) {
  const error = document.getElementById("cvErr");
  clearError(error);
  try {
    const created = await api("/api/admin/leads/" + id + "/convert", { method: "POST", json: {
      email: document.getElementById("cvEmail").value.trim(),
      display_name: document.getElementById("cvName").value.trim(),
      organisation: document.getElementById("cvOrg").value.trim(),
      monthly_quota: parseInt(document.getElementById("cvQuota").value, 10) || 0,
    }});
    showPassword(created.email, created.password, t("created"));
    loadLeads();
  } catch (failure) {
    showError(error, failure);
  }
}

/* ---------------- boot ---------------- */
(function () {
  let theme = null, lang = null;
  try { theme = localStorage.getItem("ec-theme"); lang = localStorage.getItem("ec-lang"); } catch (e) {}
  if (theme) setTheme(theme);
  else {
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme:dark)").matches;
    ["1", "2"].forEach((n) => {
      const l = document.getElementById("t-l-" + n), d = document.getElementById("t-d-" + n);
      if (l) l.setAttribute("aria-pressed", String(!dark));
      if (d) d.setAttribute("aria-pressed", String(dark));
    });
  }
  setLang(lang === "en" ? "en" : "ar");

  session = loadSession();
  if (session) enterApp();
})();
