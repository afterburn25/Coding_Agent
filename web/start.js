// Start Here — onboarding form. All validation here is UX; the backend
// re-validates everything (incl. 5-digit ZIP, creator passcode).
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const norm = (s) => String(s || "").trim().normalize("NFKC")
    .toLowerCase().replace(/\s+/g, " ");
  const api = (p) => fetch(p).then((r) => (r.ok ? r.json()
    : Promise.reject(new Error(String(r.status)))));
  const post = (p, b) => fetch(p, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(b || {}),
  }).then((r) => r.json().then((d) => (r.ok ? d
    : Promise.reject(new Error(d.error || r.status)))));

  const RESERVED = "john hamburn";

  // ---------- birthdate ----------
  const MONTHS = ["January","February","March","April","May","June",
    "July","August","September","October","November","December"];
  const y = $("birthYear"), m = $("birthMonth"), d = $("birthDay");
  m.innerHTML = `<option value="">Month</option>` +
    MONTHS.map((n, i) => `<option value="${i + 1}">${n}</option>`).join("");
  const thisYear = new Date().getFullYear();
  y.innerHTML = `<option value="">Year</option>` +
    Array.from({ length: 121 }, (_, i) => thisYear - i)
      .map((v) => `<option>${v}</option>`).join("");
  function daysInMonth() {
    const mi = +m.value, yi = +y.value || 2000; // leap-safe default
    return mi ? new Date(yi, mi, 0).getDate() : 31;
  }
  function fillDays() {
    const keep = +d.value, max = daysInMonth();
    d.innerHTML = `<option value="">Day</option>` +
      Array.from({ length: max }, (_, i) => i + 1)
        .map((v) => `<option${v === keep ? " selected" : ""}>${v}</option>`)
        .join("");
  }
  function age() {
    if (!(+m.value && +d.value && +y.value)) return null;
    const b = new Date(+y.value, m.value - 1, +d.value);
    const t = new Date();
    let a = t.getFullYear() - b.getFullYear();
    if (t.getMonth() < b.getMonth() ||
        (t.getMonth() === b.getMonth() && t.getDate() < b.getDate())) a--;
    return a;
  }
  function renderAge() {
    const a = age(), el = $("ageReadout");
    el.textContent = a == null ? "Age: —" : `Age: ${a}`;
    el.classList.toggle("adult-ok", a != null && a >= 18);
  }
  [m, y].forEach((el) => el.addEventListener("change", () => {
    fillDays(); renderAge(); validate();
  }));
  d.addEventListener("change", () => { renderAge(); validate(); });
  fillDays();

  // ---------- postal (manual ZIP — no autofill) ----------
  api("/api/postal/states").then((s) => {
    $("stateList").innerHTML = (s.states || [])
      .map((v) => `<option value="${v.code}">${v.name}</option>`).join("");
  }).catch(() => {});
  $("state").addEventListener("change", () => {
    const st = $("state").value.trim().toUpperCase();
    $("state").value = st;
    $("cityList").innerHTML = "";
    if (st.length === 2) {
      api(`/api/postal/cities?state=${encodeURIComponent(st)}`)
        .then((c) => {
          $("cityList").innerHTML = (c.cities || [])
            .map((v) => `<option value="${v.name}"></option>`).join("");
        }).catch(() => {});
    }
    validate();
  });
  $("zip").addEventListener("input", () => {
    $("zip").value = $("zip").value.replace(/\D/g, "").slice(0, 5);
    validate();
  });

  // ---------- avatar cropper ----------
  const canvas = $("cropCanvas"), ctx = canvas.getContext("2d");
  const VIEW = 240, RADIUS = 108;
  let img = null, imgURL = "", scale0 = 1, drag = { x: 0, y: 0 };
  function drawAvatar() {
    ctx.clearRect(0, 0, VIEW, VIEW);
    ctx.fillStyle = "#0a1626"; ctx.fillRect(0, 0, VIEW, VIEW);
    if (img) {
      const z = $("avatarZoom").value / 100, s = scale0 * z;
      ctx.drawImage(img, drag.x, drag.y, img.width * s, img.height * s);
    } else {
      ctx.fillStyle = "#3a4b63"; ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No photo selected", VIEW / 2, VIEW / 2);
    }
    // circular mask overlay
    ctx.save();
    ctx.fillStyle = "rgba(4,10,20,.55)";
    ctx.beginPath();
    ctx.rect(0, 0, VIEW, VIEW);
    ctx.arc(VIEW / 2, VIEW / 2, RADIUS, 0, Math.PI * 2, true);
    ctx.fill("evenodd");
    ctx.strokeStyle = "#11cfff"; ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(VIEW / 2, VIEW / 2, RADIUS, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
  function clampDrag() {
    if (!img) return;
    const z = $("avatarZoom").value / 100, s = scale0 * z;
    // keep image covering the circle
    const w = img.width * s, h = img.height * s;
    drag.x = Math.min(VIEW / 2 - RADIUS, Math.max(VIEW / 2 + RADIUS - w, drag.x));
    drag.y = Math.min(VIEW / 2 - RADIUS, Math.max(VIEW / 2 + RADIUS - h, drag.y));
  }
  function cropParams() {
    if (!img) return null;
    const z = $("avatarZoom").value / 100, s = scale0 * z;
    // circle center in image coords → fraction; zoom → backend side
    const cx = (VIEW / 2 - drag.x) / s / img.width;
    const cy = (VIEW / 2 - drag.y) / s / img.height;
    const zoom = Math.min(img.width, img.height) * s / (RADIUS * 2);
    return { cx: +cx.toFixed(4), cy: +cy.toFixed(4),
             zoom: +zoom.toFixed(4) };
  }
  let dragging = null;
  canvas.addEventListener("pointerdown", (e) => {
    dragging = { x: e.clientX - drag.x, y: e.clientY - drag.y };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dragging || !img) return;
    drag.x = e.clientX - dragging.x; drag.y = e.clientY - dragging.y;
    clampDrag(); drawAvatar();
  });
  canvas.addEventListener("pointerup", () => { dragging = null; });
  $("avatarZoom").addEventListener("input", () => { clampDrag(); drawAvatar(); });
  $("pickAvatar").addEventListener("click", () => $("avatarFile").click());
  $("avatarFile").addEventListener("change", () => {
    const f = $("avatarFile").files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => {
      imgURL = rd.result;
      const im = new Image();
      im.onload = () => {
        img = im;
        scale0 = VIEW / Math.min(im.width, im.height);
        drag = { x: (VIEW - im.width * scale0) / 2,
                 y: (VIEW - im.height * scale0) / 2 };
        $("avatarZoom").value = 100;
        clampDrag(); drawAvatar(); validate();
      };
      im.src = imgURL;
    };
    rd.readAsDataURL(f);
  });
  drawAvatar();

  // ---------- creator flow ----------
  function reserved() {
    return norm(`${$("firstName").value} ${$("lastName").value}`) === RESERVED;
  }
  function syncCreator() {
    $("creatorCard").hidden = !reserved();
    if (!reserved()) $("creatorPasscode").value = "";
  }
  ["firstName", "lastName"].forEach((id) =>
    $(id).addEventListener("input", () => { syncCreator(); validate(); }));

  // ---------- validation ----------
  const EMAIL = /^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$/;
  function valid() {
    const req = ["firstName", "lastName", "sex", "birthMonth", "birthDay",
      "birthYear", "email", "phone", "street", "state", "city"];
    if (req.some((id) => !$(id).value.trim())) return false;
    if (!EMAIL.test($("email").value.trim())) return false;
    if (!/^\d{5}$/.test($("zip").value)) return false;
    const a = age();
    if (a == null || a < 0 || a > 130) return false;
    if (!img) return false;
    if (reserved() && !$("creatorPasscode").value.trim()) return false;
    return true;
  }
  function validate() { $("createBtn").disabled = !valid(); }
  document.querySelectorAll("#profileForm input,#profileForm select")
    .forEach((el) => el.addEventListener("input", validate));

  // ---------- submit ----------
  $("profileForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("formError");
    err.hidden = true;
    const btn = $("createBtn");
    btn.disabled = true; btn.textContent = "Creating profile…";
    const body = {
      first_name: $("firstName").value.trim(),
      last_name: $("lastName").value.trim(),
      sex: $("sex").value,
      birth_date: `${y.value}-${String(m.value).padStart(2, "0")}-${String(d.value).padStart(2, "0")}`,
      email: $("email").value.trim(),
      phone: $("phone").value.trim(),
      street_address: $("street").value.trim(),
      city: $("city").value.trim(),
      state: $("state").value.trim().toUpperCase(),
      zip_code: $("zip").value,
      avatar: { data_url: imgURL, crop: cropParams() },
    };
    if (reserved()) body.creator_passcode = $("creatorPasscode").value;
    try {
      const r = await post("/api/profiles", body);
      if (r.greeting && r.greeting.text) {
        $("introText").textContent = r.greeting.text;
        $("introOverlay").hidden = false;
      } else {
        location.href = "/";
      }
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      btn.disabled = false;
      btn.textContent = "Create Profile & Unlock Nexus";
    }
  });
  $("introDone").addEventListener("click", () => { location.href = "/"; });
})();
