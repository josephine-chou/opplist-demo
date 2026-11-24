// The quotation PDF (attached to the draft): drawn in the browser with jsPDF (vendor/, loaded the first
// time it is needed; nothing is sent anywhere), from quotemail.sheetOf(). Letter. The template:
// the logo; Quotation and the line under it, the number, the day issued and valid until on the
// right; then, each with its label on the left, the parties, the shipment, the charges and their total, remarks; the
// terms in two columns; our address and the page at the foot. Its fonts, Inter and Newsreader (SIL Open Font
// License, vendor/fonts/), are read the first time a PDF is made; without them it is drawn in Helvetica and Times.
// Only the sell prices: sheetOf() has no cost, markup or margin.
const JSPDF = "vendor/jspdf-4.2.1.umd.min.js";
let loading = null;
export function loadPdf() {
  if (window.jspdf?.jsPDF) return Promise.resolve(window.jspdf.jsPDF);
  if (!loading) loading = new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = JSPDF; s.async = true;
    s.onload = () => window.jspdf?.jsPDF ? res(window.jspdf.jsPDF) : (loading = null, rej(new Error("The PDF maker did not start.")));
    s.onerror = () => { loading = null; s.remove(); rej(new Error("Could not load the PDF maker. Check the connection and try again.")); };
    document.head.appendChild(s);
  });
  return loading;
}

// the template fonts as jsPDF takes them (base64), read once
const FONTS = [["Inter", "normal", "Inter-Regular.ttf"], ["InterMedium", "normal", "Inter-Medium.ttf"],
  ["InterSemiBold", "normal", "Inter-SemiBold.ttf"], ["Newsreader", "normal", "Newsreader-Regular.ttf"], ["Newsreader", "italic", "Newsreader-Italic.ttf"]];
let fonts = null;
const base64 = buf => {
  const a = new Uint8Array(buf); let s = "";
  for (let i = 0; i < a.length; i += 0x8000) s += String.fromCharCode.apply(null, a.subarray(i, i + 0x8000));
  return btoa(s);
};
function fontData() {
  if (!fonts) fonts = Promise.all(FONTS.map(async ([, , file]) => {
    const r = await fetch(`vendor/fonts/${file}`);
    if (!r.ok) throw new Error(`${file}: ${r.status}`);
    return base64(await r.arrayBuffer());
  })).catch(e => { fonts = null; throw e; });
  return fonts;
}

// the standard PDF fonts write WinAnsi only: a character outside it becomes "?" rather than garbage. The template fonts
// have all of Latin-1 and most of the rest; the few letters they lack are written plain (Š as S)
const WINANSI_EXTRA = new Set("€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ");
const PLAIN = { "Š": "S", "š": "s", "Ž": "Z", "ž": "z", "Ÿ": "Y", "ƒ": "f", "†": "+", "‡": "+", "‰": "%", "­": "" };
export function pdfText(s, embedded = false) {
  return [...String(s ?? "").normalize("NFC")].map(ch => {
    const c = ch.codePointAt(0);
    if (ch === "\t") return " ";
    if (c < 32) return "";
    if (embedded && ch in PLAIN) return PLAIN[ch];
    return c <= 255 || WINANSI_EXTRA.has(ch) ? ch : "?";
  }).join("");
}

// the template, in points (its own measures)
const WORDMARK = "JoJo Freight";
const INK = [22, 22, 22], MUTED = [95, 95, 95], SUB = [63, 63, 63], TERMS_INK = [51, 51, 51], BLUE = [0, 116, 163];
const RULE = [22, 22, 22], SEP = [220, 220, 220], ROWLINE = [236, 236, 236];
const W = 612, L = 48, R = W - 48, C = 165, BOTTOM = 730, FOOT = 762.75, NEXT_TOP = 62;
const HALF = 190.5, CELL = 139, BASIS = 360, RATE_R = 495;

export async function quotePdf(sheet) {
  const jsPDF = await loadPdf();
  const doc = new jsPDF({ unit: "pt", format: "letter", compress: true });
  let embedded = false;
  try {
    const data = await fontData();
    FONTS.forEach(([family, style, file], i) => { doc.addFileToVFS(file, data[i]); doc.addFont(file, family, style); });
    embedded = true;
  } catch { embedded = false; }
  const F = embedded
    ? { serif: ["Newsreader", "normal"], italic: ["Newsreader", "italic"], reg: ["Inter", "normal"], med: ["InterMedium", "normal"], semi: ["InterSemiBold", "normal"] }
    : { serif: ["times", "normal"], italic: ["times", "italic"], reg: ["helvetica", "normal"], med: ["helvetica", "normal"], semi: ["helvetica", "bold"] };
  const no = sheet.no || sheet.ref;
  doc.setProperties({ title: `JoJo Quotation ${no}`, subject: `Quotation ${no}`, author: "JoJo Freight (CHI) Inc.", creator: "OppList team site" });
  const T = s => pdfText(s, embedded);
  const font = (f, size, color = INK) => { doc.setFont(...F[f]); doc.setFontSize(size); doc.setTextColor(...color); };
  const widthOf = (s, f, size) => { font(f, size); return doc.getTextWidth(T(s)); };
  const wrap = (s, w) => doc.splitTextToSize(T(s), w);          // in the font set last
  // whole words only (a rate's word, AT COST or IF APPLICABLE, is never cut in two); in the font set last
  const words = (s, w) => {
    const out = []; let line = "";
    for (const word of T(s).split(/\s+/).filter(Boolean)) {
      const next = line ? `${line} ${word}` : word;
      if (line && doc.getTextWidth(next) > w) { out.push(line); line = word; } else line = next;
    }
    return line ? [...out, line] : out;
  };
  const rule = (y, x1, x2, color) => { doc.setDrawColor(...color); doc.setLineWidth(0.75); doc.line(x1, y, x2, y); };
  const label = (t, y, x = L, cs = 1.1) => { font("reg", 9, MUTED); doc.text(T(t).toUpperCase(), x, y, { charSpace: cs }); };
  const newPage = () => { doc.addPage(); return NEXT_TOP; };
  // a bold name and the rest after it on the same line, both wrapped to the column ("Name · Attn: person")
  const rich = (x, y0, w, bold, rest, lh = 15) => {
    let yy = y0, cx = x;
    const put = (text, f) => {
      font(f, 9.75, INK);
      for (const tok of T(text).split(/(\s+)/)) {
        if (!tok) continue;
        const tw = doc.getTextWidth(tok);
        if (!tok.trim()) { if (cx > x) cx += tw; continue; }
        if (cx > x && cx + tw > x + w) { yy += lh; cx = x; }
        if (tw > w) {
          for (const part of doc.splitTextToSize(tok, w)) { if (cx > x) { yy += lh; cx = x; } doc.text(part, x, yy); cx = x + doc.getTextWidth(part); }
          continue;
        }
        doc.text(tok, cx, yy); cx += tw;
      }
    };
    if (bold) put(bold, "semi");
    if (rest) put(rest, "reg");
    return yy;
  };

  // ---- the head: the logo, Quotation and the line under it; the number and the dates on the right
  // the company's name as a wordmark: type and one rule under it (this copy ships no image)
  font("semi", 15, BLUE); doc.text(WORDMARK, L, 54);
  doc.setDrawColor(...BLUE); doc.setLineWidth(1.5); doc.line(L, 60, L + widthOf(WORDMARK, "semi", 15), 60);
  const right = [["No.", no, "semi", INK], ["Issued", sheet.date, "reg", INK], ["Valid until", sheet.valid, "semi", BLUE]];
  const vw = Math.max(...right.map(([, v, f]) => widthOf(v, f, 9.375))), kw = Math.max(...right.map(([k]) => widthOf(k, "reg", 9.375)));
  const kx = R - vw - 12 - kw;
  right.forEach(([k, v, f, color], i) => {
    font("reg", 9.375, MUTED); doc.text(k, kx, 101.25 + i * 13.5);
    font(f, 9.375, color); doc.text(T(v), R, 101.25 + i * 13.5, { align: "right" });
  });
  font("serif", 35, INK); doc.text("Quotation", L, 105);                          // 39 pt in the template
  let sep = 150.35;
  if (sheet.title) {
    font("italic", 13.5, SUB);
    const lines = wrap(sheet.title, kx - 18 - L);
    lines.forEach((s, i) => doc.text(s, L, 129.75 + i * 17));
    sep += (lines.length - 1) * 17;
  }
  rule(sep, L, R, RULE);

  // ---- the parties: who it is for, who it is from
  const P = sheet.parties || { for: {}, from: {} };
  let y = sep + 23.65;
  label("Parties", y - 0.75);
  const party = (x, head, bold, rest, email) => {
    font("reg", 9.75, MUTED); doc.text(head, x, y);
    let yy = bold || rest ? rich(x, y + 15, HALF, bold, rest) : y;
    if (email) { font("reg", 9.75, INK); for (const s of wrap(email, HALF)) { yy += 15; doc.text(s, x, yy); } }
    return yy;
  };
  const fo = P.for || {}, fr = P.from || {};
  const last = Math.max(
    party(C, "For", fo.name, fo.attn ? `${fo.name ? " · " : ""}Attn: ${fo.attn}` : "", fo.email),
    party(C + HALF + 18, "From", fr.name || fr.company, "", fr.email));       // the person and the mail, no company
  sep = last + 16.5; rule(sep, L, R, SEP);

  // ---- the shipment, three to a row
  const grid = sheet.grid || [];
  if (grid.length) {
    y = sep + 22;
    label("Shipment", y + 1.5);
    for (let i = 0; i < grid.length; i += 3) {
      let lines = 1;
      grid.slice(i, i + 3).forEach(([k, v], j) => {
        const x = C + j * CELL;
        font("reg", 9, MUTED); doc.text(T(k), x, y);
        font("med", 9.75, INK); const vs = wrap(v, CELL - 12);
        vs.forEach((s, n) => doc.text(s, x, y + 13.5 * (n + 1)));
        lines = Math.max(lines, vs.length);
      });
      y += 13.5 * lines + (i + 3 < grid.length ? 21.75 : 0);
    }
    sep = y + 15.75; rule(sep, L, R, SEP);
  }

  // ---- the charges: description, basis, rate, amount; the total under them
  const heads = yy => {
    font("med", 9, MUTED);
    doc.text("Description", C, yy); doc.text("Basis", BASIS, yy);
    doc.text("Rate", RATE_R, yy, { align: "right" }); doc.text("Amount", R, yy, { align: "right" });
    rule(yy + 7.85, C, R, RULE);
    return yy + 8.25;                                    // the top of the first row
  };
  y = sep + 21.8;
  label("Charges", y + 1.5);
  let top = heads(y);
  const rows = sheet.charges || [];
  rows.forEach((r, i) => {
    font("reg", 9.75); const dl = wrap(r.desc, BASIS - C - 10), bl = words(r.basis, 78), ra = words(r.rate, 58), am = words(r.amount, 66);
    font("reg", 8.25); const rl = r.remark ? wrap(r.remark, BASIS - C - 10) : [];
    const n = Math.max(dl.length, bl.length, ra.length, am.length, 1);
    const span = Math.max((n - 1) * 12.75, (dl.length - 1) * 12.75 + rl.length * 11.5);
    if (top + 15 + span + 12.75 > BOTTOM) top = heads(newPage());
    const b = top + 15;
    font("reg", 9.75, INK); dl.forEach((s, k) => doc.text(s, C, b + k * 12.75));
    ra.forEach((s, k) => doc.text(s, RATE_R, b + k * 12.75, { align: "right" }));
    am.forEach((s, k) => doc.text(s, R, b + k * 12.75, { align: "right" }));
    font("reg", 9.75, MUTED); bl.forEach((s, k) => doc.text(s, BASIS, b + k * 12.75));
    font("reg", 8.25, MUTED); rl.forEach((s, k) => doc.text(s, C, b + (dl.length - 1) * 12.75 + 11.5 * (k + 1)));
    if (i < rows.length - 1) { rule(b + span + 8.6, C, R, ROWLINE); top = b + span + 9; }
    else { rule(b + span + 12.35, C, R, RULE); top = b + span + 12.75; }
  });
  if (!rows.length) { rule(top + 4, C, R, RULE); top += 4.4; }
  let tb = top + 24.75;
  if (tb + (sheet.total?.atCost ? 15 : 0) > BOTTOM) { const t0 = newPage(); rule(t0, C, R, RULE); tb = t0 + 24.75; }
  label("Total", tb, C, 0.9);
  const amount = T(sheet.total?.amount ?? "");
  font("serif", 19, INK); const aw = doc.getTextWidth(amount); doc.text(amount, R, tb, { align: "right" });
  font("reg", 10.5, MUTED); doc.text(T(sheet.cur), R - aw - 6, tb, { align: "right" });
  if (sheet.total?.atCost) { tb += 15; font("reg", 8.25, MUTED); doc.text("Plus the AT COST lines.", R, tb, { align: "right" }); }
  sep = tb + 25.1; rule(sep, L, R, SEP);

  // ---- remarks
  if (sheet.remarks) {
    font("reg", 9.75, INK);
    const lines = wrap(sheet.remarks, R - C);
    y = sep + 23.65;
    if (y + 14.5 * Math.min(lines.length - 1, 2) > BOTTOM) y = newPage();
    label("Remarks", y - 0.75);
    lines.forEach((s, k) => {
      if (k) { y += 14.5; if (y > BOTTOM) y = newPage(); }
      font("reg", 9.75, INK); doc.text(s, C, y);
    });
    sep = y + 16.5; rule(sep, L, R, SEP);
  }

  // ---- the terms, in two columns balanced as the template does (the first one no shorter than the second); when
  // they do not fit under the rest, a size smaller (down to 7.5 pt) before they go to a page of their own
  const terms = sheet.terms || [];
  if (terms.length) {
    const colW = 251.3, gap = 13.5, ind = 13.5;
    const layout = size => {
      const lh = 12.75 * size / 9, sp = 2.25 * size / 9;
      font("reg", size, TERMS_INK);
      const items = terms.map((t, i) => ({ n: `${i + 1}.`, lines: wrap(t, colW - ind) }));
      const hOf = list => list.reduce((a, it) => a + it.lines.length * lh, 0) + Math.max(0, list.length - 1) * sp;
      let k = items.length, best = Infinity;
      for (let j = items.length; j >= Math.ceil(items.length / 2); j--) {
        const hl = hOf(items.slice(0, j)), hr = hOf(items.slice(j)), m = Math.max(hl, hr);
        if (hl >= hr && m <= best) { best = m; k = j; }
      }
      return { size, lh, sp, items, k, height: best - lh };   // the first line's baseline to the last one's
    };
    let lab = sep + 20.25;
    let t = [9, 8.5, 8, 7.5].map(layout).find(x => lab + 18 + x.height <= BOTTOM);
    if (!t) { lab = newPage(); t = layout(9); }
    label("Terms", lab);
    const numEdge = 2.8 + widthOf("1.", "reg", t.size);
    [t.items.slice(0, t.k), t.items.slice(t.k)].forEach((col, c) => {
      const x0 = L + c * (colW + gap);
      let yy = lab + 18;
      col.forEach(it => {
        font("reg", t.size, TERMS_INK);
        doc.text(it.n, x0 + numEdge, yy, { align: "right" });
        it.lines.forEach((s, j) => doc.text(s, x0 + ind, yy + j * t.lh));
        yy += it.lines.length * t.lh + t.sp;
      });
    });
  }

  // ---- the foot of every page: our address, the page
  const pages = doc.getNumberOfPages();
  for (let i = 1; i <= pages; i++) {
    doc.setPage(i);
    font("reg", 9, MUTED);
    doc.text(T(sheet.company), L, FOOT);
    doc.text(`${i} / ${pages}`, R, FOOT, { align: "right" });
  }
  return doc.output("blob");
}

// the file to the person's Downloads (or wherever the browser saves files)
export function downloadBlob(blob, name) {
  const a = document.createElement("a"), url = URL.createObjectURL(blob);
  a.href = url; a.download = name; a.style.display = "none";
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 2000);
}
// the Preview: the same PDF in a new tab
export function openBlob(blob) {
  const url = URL.createObjectURL(blob), w = window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  return !!w;
}
