// The part of OppList that this copy of the site cannot wait for. On a real installation OppList runs on
// one computer, pushes the lists every few minutes and takes in what people did on the site. Here nobody
// pulls: an action stays "not in the lists yet" and the pages show it laid over the lists, which is the
// state the real site is in between two syncs.
//
// Two things would look broken if nothing answered, so this module answers them:
//   - the time of the last update moves on, so the page does not call the lists stale after 20 minutes;
//   - a Read quote request gets the reading recorded for the demo (see onWrite).
import { nowMs } from "../lib.js";
import { peek, pushAsOppList, Timestamp } from "./firestore.js";

const BEAT_MS = 4 * 60000;
let timer = null;

function beat() {
  const meta = peek("meta/sync");
  if (!meta) return;
  const at = new Timestamp(nowMs());
  meta.pushedAt = at;
  if (meta.readQuote) meta.readQuote = { ...meta.readQuote, appAt: at };
  pushAsOppList("meta/sync", meta, { kept: false });
}

export const standIn = {
  start() {
    if (timer) clearInterval(timer);
    timer = setInterval(beat, BEAT_MS);
    if (timer.unref) timer.unref();   // Node: the test ends although the timer is set
  },
  stop() { if (timer) clearInterval(timer); timer = null; },
  beat,
  // called by the store after every write of the site: (collection, id, fields)
  onWrite(col, id, fields) {
    if (col === "actions" && fields.type === "read") answerRead(id, fields);
  },
};

// Read quote: the site sends the vendor's mail (and files) as a `read` action; OppList reads it, writes a
// draft of the rates (rateDrafts) and answers the action (actionResults: "queued", then "done"). The demo
// has no reader. It answers every request with the one reading OppList made from a recorded answer when the
// snapshot was built (demo/readAnswer), a moment later, whatever was sent: the screens after a reading are
// real, the reading itself is a recording.
const READ_DELAY_MS = 2500;
function answerRead(id, action) {
  const rec = peek("demo/readAnswer");
  const base = { type: "read", opp: action.opp, by: action.by, vendor: action.vendor || "" };
  pushAsOppList(`actionResults/${id}`, { ...base, state: "queued", at: new Timestamp(nowMs()) });
  setTimeout(() => {
    const at = new Timestamp(nowMs());
    if (!rec || !rec.draft) {
      pushAsOppList(`actionResults/${id}`, { ...base, state: "done", ok: false, msg: "This demo has no recorded reading.", at });
      return;
    }
    pushAsOppList(`rateDrafts/demo-${id}`, { ...rec.draft, opp: action.opp, vendor: action.vendor || "", readAt: at, status: "open", savedAs: "", taken: (rec.draft.options || [""]).map(() => "") });
    pushAsOppList(`actionResults/${id}`, { ...base, state: "done", ok: true, msg: "Read from the demo's recorded answer.", at });
  }, READ_DELAY_MS);
}
