import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

const REASONS = {
  clinical_urgent: ["Clinical urgent", "red"],
  medical_advice: ["Medical advice", "red"],
  not_authorised: ["Not authorised", "amber"],
  ambiguous_patient: ["Ambiguous patient", "amber"],
  out_of_scope: ["Out of scope", "grey"],
};
const STATE_TONE = { booked: "green", rescheduled: "green", cancelled: "green", escalated: "red", refused: "grey", abandoned: "grey" };
const hhmm = (iso) => (iso || "").slice(11, 16);

function useHashRoute() {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const [, a, b] = hash.split("/");
  if (a === "c" && b) return { name: "detail", id: decodeURIComponent(b) };
  if (a === "conversations") return { name: "list" };
  if (a === "try") return { name: "try" };
  return { name: "queue" };
}

function useLoad(fn, deps) {
  const [state, setState] = useState({ data: null, error: "", loading: true });
  const load = useCallback(() => {
    fn().then((data) => setState({ data, error: "", loading: false })).catch((e) => setState((s) => ({ ...s, error: e.message, loading: false })));
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setState({ data: null, error: "", loading: true }); load(); }, [load]);
  return { ...state, reload: load };
}

function Pill({ tone, children }) { return <span className={`pill ${tone}`}>{children}</span>; }
const ReasonPill = ({ reason }) => { const [t, c] = REASONS[reason] || [reason, "grey"]; return <Pill tone={c}>{t}</Pill>; };

function Sidebar({ route }) {
  const items = [["#/", "Handoff Queue", "queue"], ["#/conversations", "Conversations", "list"], ["#/try", "Try the agent", "try"]];
  const active = route.name === "detail" ? "list" : route.name;
  return (
    <nav className="sidebar" aria-label="Main">
      <div className="brand">Swasthi<span>Q</span></div>
      {items.map(([href, label, key]) => (
        <a key={key} href={href} className={"navitem" + (active === key ? " active" : "")} aria-current={active === key ? "page" : undefined}>
          <span className="dot" />{label}
        </a>
      ))}
    </nav>
  );
}

function Header({ title, sub, right }) {
  return (<header className="pagehead"><div><h1>{title}</h1><p className="sub">{sub}</p></div>{right}</header>);
}
const Status = ({ error, loading }) => error ? <p className="error" role="alert">{error}</p> : loading ? <p className="muted">Loading…</p> : null;

function Stat({ label, value, sub, accent }) {
  return (<div className="stat"><div className="label">{label}</div><div className="value">{value}</div><div className={"substat" + (accent ? " accent" : "")}>{sub}</div></div>);
}

/* ---------- Screen 1: Handoff Queue ---------- */
function HandoffQueue() {
  const stats = useLoad(api.stats, []);
  const list = useLoad(api.handoffs, []);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");

  const resolve = async (id) => {
    setBusy(id); setActionError("");
    try { await api.resolve(id); } catch (e) { setActionError(e.message); }
    setBusy(""); stats.reload(); list.reload();
  };
  const s = stats.data, rows = list.data;
  return (<>
    <Header title="Handoff Queue" sub="Sunrise Clinic, Dehradun · conversations the agent passed to a human"
            right={s && <span className="badge">{s.open} open</span>} />
    <Status error={stats.error || list.error} loading={stats.loading && list.loading} />
    {actionError && <p className="error" role="alert">{actionError}</p>}
    {s && (
      <div className="stats">
        <Stat label="Conversations" value={s.conversations} sub="handled" />
        <Stat label="Completed by agent" value={s.completed} sub={`${s.completed_pct}%`} />
        <Stat label="Escalated" value={s.escalated} sub={`${s.open} still open`} accent />
        <Stat label="Urgent" value={s.urgent_open} sub="clinical, unresolved" accent />
      </div>
    )}
    {rows && (
      <section className="card">
        <h2>Open handoffs</h2>
        <div className="scroll">
          <table>
            <thead><tr><th>Conversation</th><th>Caller said</th><th>Reason</th><th>Time</th><th /></tr></thead>
            <tbody>
              {rows.length === 0 && <tr><td colSpan="5" className="muted">Nothing waiting. The queue is clear.</td></tr>}
              {rows.map((r, i) => (
                <tr key={r.id} className={r.escalation_reason === "clinical_urgent" ? "urgent" : ""}>
                  <td className="mono"><a href={`#/c/${encodeURIComponent(r.id)}`}>{r.id}</a></td>
                  <td className="said">{r.caller_said || "—"}</td>
                  <td><ReasonPill reason={r.escalation_reason} /></td>
                  <td className="muted">{hhmm(r.created_at)}</td>
                  <td className="right">
                    <button className={"btn" + (i === 0 ? " primary" : "")} disabled={busy === r.id} onClick={() => resolve(r.id)}>
                      {busy === r.id ? "Resolving…" : "Resolve"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    )}
  </>);
}

/* ---------- Screen 2: Conversation Detail ---------- */
function ToolRow({ e }) {
  const args = Object.entries(e.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
  return (
    <div className="row">
      <div className="who">Tool</div>
      <div className={"bubble tool" + (e.ok ? "" : " failed")}>
        <b className="mono">{e.name}</b><span className="mono dim">({args})</span>
        <div className="mono result">→ {e.summary}</div>
      </div>
    </div>
  );
}

function ConversationDetail({ id }) {
  const { data: d, error, loading } = useLoad(() => api.conversation(id), [id]);
  if (error || loading) return <><Header title={`Conversation ${id}`} sub="" /><Status error={error} loading={loading} /></>;
  const r = d.result, m = r.metrics;
  const toolCount = d.transcript.filter((e) => e.kind === "tool").length;
  const badge = d.terminal_state === "escalated" ? `Escalated · ${(REASONS[d.escalation_reason] || [d.escalation_reason])[0]}` : d.terminal_state;
  const KV = ({ k, v }) => (<div className="kv"><span>{k}</span><b className="mono">{String(v)}</b></div>);
  return (<>
    <Header title={`Conversation ${d.id}`} sub={`${d.clinic} · ${d.today} · ${hhmm(d.created_at)}`}
            right={<Pill tone={STATE_TONE[d.terminal_state] || "grey"}>{badge}</Pill>} />
    <div className="two">
      <section className="card">
        <h2>Transcript and tool calls</h2>
        {d.transcript.map((e, i) => e.kind === "tool" ? <ToolRow key={i} e={e} /> : e.kind === "guard" ? (
          <div className="note danger" key={i}>{e.text}</div>
        ) : (
          <div className="row" key={i}>
            <div className="who">{e.kind === "caller" ? "Caller" : "Agent"}</div>
            <div className={"bubble " + e.kind}>{e.text}</div>
          </div>
        ))}
        <div className={"note " + d.note.tone}>{d.note.text}</div>
      </section>
      <section className="card">
        <h2>Outcome</h2>
        <KV k="terminal_state" v={r.terminal_state} />
        <KV k="escalation_reason" v={r.escalation_reason} />
        <KV k="patient_id" v={r.patient_id} />
        <KV k="appointment_id" v={r.appointment_id} />
        <KV k="tool_calls" v={toolCount} />
        <KV k="turns" v={m.turns} />
        <KV k="tokens" v={m.tokens.toLocaleString()} />
        <KV k="latency" v={`${m.latency_ms} ms`} />
        <div className="det">
          <div className="label">Determinism</div>
          <p>Identical result across {d.determinism.runs} run{d.determinism.runs === 1 ? "" : "s"}. <Pill tone={d.determinism.stable ? "green" : "red"}>{d.determinism.stable ? "Stable" : "Unstable"}</Pill></p>
        </div>
      </section>
    </div>
  </>);
}

/* ---------- Supporting screens ---------- */
function Conversations() {
  const { data, error, loading } = useLoad(api.conversations, []);
  return (<>
    <Header title="Conversations" sub="Every conversation the agent has handled" />
    <Status error={error} loading={loading} />
    {data && (
      <section className="card"><div className="scroll"><table>
        <thead><tr><th>Conversation</th><th>Caller said</th><th>Outcome</th><th>Time</th></tr></thead>
        <tbody>{data.map((r) => (
          <tr key={r.id}>
            <td className="mono"><a href={`#/c/${encodeURIComponent(r.id)}`}>{r.id}</a></td>
            <td className="said">{r.caller_said || "—"}</td>
            <td>{r.escalation_reason ? <ReasonPill reason={r.escalation_reason} /> : <Pill tone={STATE_TONE[r.terminal_state] || "grey"}>{r.terminal_state}</Pill>}{r.resolved && <span className="muted"> · resolved</span>}</td>
            <td className="muted">{hhmm(r.created_at)}</td>
          </tr>))}
        </tbody></table></div></section>
    )}
  </>);
}

function Try() {
  const { data: scripts } = useLoad(api.scripts, []);
  const [turns, setTurns] = useState("Dr. Rao ke saath kal ka appointment chahiye tha.\nSubah 10 baje.\nWaise abhi seene mein dard ho raha hai aur saans thodi phool rahi hai.");
  const [today, setToday] = useState("2026-10-01");
  const [out, setOut] = useState(null), [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setErr("");
    try {
      const id = "try_" + Date.now().toString(36);
      setOut(await api.run({ conversation_id: id, today, turns: turns.split("\n").filter((t) => t.trim()) }));
      setOut((o) => ({ ...o, _id: id }));
    } catch (e) { setErr(e.message); setOut(null); }
    setBusy(false);
  };
  return (<>
    <Header title="Try the agent" sub="Each run starts from the clinic data as shipped. One caller turn per line. Break it." />
    <section className="card">
      <div className="controls">
        <label>Load a script{" "}
          <select defaultValue="" onChange={(e) => { const s = scripts?.find((x) => x.id === e.target.value); if (s) { setTurns(s.turns.join("\n")); setToday(s.today); } }}>
            <option value="">— choose —</option>
            {scripts?.map((s) => <option key={s.id} value={s.id}>{s.id} · {s.description.slice(0, 56)}</option>)}
          </select>
        </label>
        <label>today <input value={today} onChange={(e) => setToday(e.target.value)} size="10" /></label>
      </div>
      <textarea rows="7" value={turns} onChange={(e) => setTurns(e.target.value)} aria-label="Caller turns, one per line" />
      <button className="btn primary" onClick={run} disabled={busy}>{busy ? "Running…" : "Run conversation"}</button>
      {err && <p className="error" role="alert">{err}</p>}
      {out && (<div className="result-box">
        <div className="kv"><span>terminal_state</span><b className="mono">{out.terminal_state}</b></div>
        <div className="kv"><span>escalation_reason</span><b className="mono">{String(out.escalation_reason)}</b></div>
        <div className="kv"><span>appointment_id</span><b className="mono">{String(out.appointment_id)}</b></div>
        <div className="kv"><span>tools called</span><b className="mono">{out.tool_calls.map((c) => c.name).join(", ") || "none"}</b></div>
        <p className="bubble agent">{out.reply}</p>
        <a href={`#/c/${out._id}`}>Open full conversation →</a>
      </div>)}
    </section>
  </>);
}

export default function App() {
  const route = useHashRoute();
  return (
    <div className="shell">
      <Sidebar route={route} />
      <main>
        {route.name === "queue" && <HandoffQueue />}
        {route.name === "detail" && <ConversationDetail id={route.id} />}
        {route.name === "list" && <Conversations />}
        {route.name === "try" && <Try />}
      </main>
    </div>
  );
}
