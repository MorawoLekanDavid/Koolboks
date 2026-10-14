import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, requestId } from "./api";
import type { Audit, Channel, Contact, Conversation, Message, Note, Product, Settings, User } from "./types";

const time = (value: number) => new Date(value * 1000).toLocaleString();
const pages = ["Overview", "Inbox", "Customers", "Catalog", "Channels", "Team", "AI & settings", "Audit"];

function ErrorBox({ error }: { error: unknown }) {
  return error ? <div className="error" role="alert">{error instanceof Error ? error.message : "Something went wrong. Please try again."}</div> : null;
}
function Empty({ children }: { children: ReactNode }) { return <div className="empty">{children}</div>; }
function useData<T>(user: User, key: string, path: string, polling = false) {
  return useQuery({ queryKey: [user.workspace_id, user.id, key], queryFn: ({ signal }) => api<T>(path, "GET", undefined, signal), refetchInterval: polling ? 5000 : false });
}
function useWrite<T>(user: User, fn: (value: T) => Promise<unknown>) {
  const cache = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => cache.invalidateQueries({ queryKey: [user.workspace_id, user.id] }) });
}

export function App() {
  const cache = useQueryClient();
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [page, setPage] = useState("Overview");
  const [error, setError] = useState<unknown>(null);
  const [away, setAway] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api<User>("/api/auth/me", "GET", undefined, controller.signal).then(setUser).catch(e => {
      if (!(e instanceof ApiError && e.status === 401) && e.name !== "AbortError") setError(e);
    }).finally(() => setBooting(false));
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!user) return;
    const heartbeat = () => api("/api/presence?away=" + away, "POST").catch(e => {
      if (e instanceof ApiError && e.status === 401) { cache.clear(); setUser(null); }
    });
    void heartbeat();
    const timer = window.setInterval(heartbeat, 20000);
    return () => clearInterval(timer);
  }, [user?.id, away, cache]);
  async function signout() {
    try { await api("/api/auth/logout", "POST"); }
    catch (e) { setError(e); return; }
    await cache.cancelQueries(); cache.clear(); setUser(null); setPage("Overview");
  }
  if (booting) return <Empty>Opening your workspace…</Empty>;
  if (!user) return <Login onLogin={async next => { await cache.cancelQueries(); cache.clear(); setUser(next); setError(null); }} error={error} />;
  const allowed = user.role === "admin" ? pages : pages.slice(0, 4);
  return <div className="app"><aside className="rail">
    <a href="#" className="brand" onClick={e => { e.preventDefault(); setPage("Overview"); }}><span className="brand-icon">i</span> itura<span className="brand-dot">.</span></a>
    <div className="rail-label">CUSTOMER OPERATIONS</div>
    <nav aria-label="Main navigation">{allowed.map((p, i) => <button key={p} className={page === p ? "nav active" : "nav"} onClick={() => { setPage(p); setError(null); }}><span aria-hidden>{["◫", "◉", "◎", "▦", "⌁", "◇", "✳", "≡"][i]}</span>{p}</button>)}</nav>
    <div className="rail-bottom"><div className="avatar">{user.name.slice(0, 1)}</div><strong>{user.name}</strong><small>{user.role}</small><button onClick={signout} className="logout">Sign out</button></div>
  </aside><div className="workspace"><header className="topbar"><span>Workspace / <strong>{page}</strong></span><button className="presence" aria-pressed={away} onClick={() => setAway(v => !v)}><i className={away ? "away" : "online"} />{away ? "Away" : "Available"}</button></header>
    <main><div className="page-heading"><div className="eyebrow">ITURA WORKSPACE</div><h1>{page}</h1><p>{({ Overview: "Keep every customer conversation moving.", Inbox: "One workspace for WhatsApp and website conversations.", Customers: "The people behind your conversations.", Catalog: "Approved products and prices, shared with your team.", Channels: "Connect accounts and control where conversations arrive.", Team: "Manage the people and permissions in this workspace.", "AI & settings": "Publish approved knowledge and control automation.", Audit: "A record of important changes and actions." } as Record<string, string>)[page]}</p></div>
    <ErrorBox error={error} />
    {page === "Overview" && <Overview user={user} onInbox={() => setPage("Inbox")} />}
    {page === "Inbox" && <Inbox user={user} />}
    {page === "Customers" && <Customers user={user} />}
    {page === "Catalog" && <Catalog user={user} />}
    {page === "Channels" && user.role === "admin" && <Channels user={user} />}
    {page === "Team" && user.role === "admin" && <Team user={user} />}
    {page === "AI & settings" && user.role === "admin" && <AiSettings user={user} />}
    {page === "Audit" && user.role === "admin" && <AuditPage user={user} />}
    </main><footer>Itura · Customer operations<span>Private workspace</span></footer></div></div>;
}

function Login({ onLogin, error }: { onLogin: (user: User) => Promise<void>; error: unknown }) {
  const [email, setEmail] = useState(""); const [password, setPassword] = useState("");
  const login = useMutation({ mutationFn: () => api<User>("/api/auth/login", "POST", { email, password }), onSuccess: onLogin });
  return <div className="login-shell"><div className="login-story"><div className="brand">itura.</div><div><div className="eyebrow">A BETTER CUSTOMER EXPERIENCE</div><h1>Every conversation.<br />A clear next step.</h1><p>Your customers, team and AI assistant—working together in one place.</p></div><small>Customer operations, thoughtfully connected.</small></div><form className="login-card" onSubmit={e => { e.preventDefault(); login.mutate(); }}><div className="eyebrow">WELCOME BACK</div><h2>Sign in to your workspace</h2><p>Use the account provided by your administrator.</p><label>Email<input type="email" autoComplete="username" required value={email} onChange={e => setEmail(e.target.value)} /></label><label>Password<input type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} /></label><ErrorBox error={login.error || error} /><button className="primary" disabled={login.isPending}>{login.isPending ? "Signing in…" : "Sign in →"}</button></form></div>;
}

function Overview({ user, onInbox }: { user: User; onInbox: () => void }) {
  const query = useData<Record<string, number>>(user, "overview", "/api/overview", true);
  const labels: Record<string, string> = { conversations: "Conversations", attention: "Need attention", unassigned: "Unassigned", open: "Open", closed: "Closed", pending_sends: "Queued messages", delivery_unknown: "Delivery needs review" };
  return <><ErrorBox error={query.error} /><div className="metrics">{Object.entries(labels).map(([key, label]) => <div className="metric" key={key}><small>{label}</small><strong>{query.data ? query.data[key] : "—"}</strong><span>{query.data ? "From recorded activity" : "Loading…"}</span></div>)}</div><div className="welcome-banner"><div><div className="eyebrow">YOUR NEXT ACTION</div><h2>Give customers a clear response.</h2><p>Review unassigned conversations and requests that need a person.</p></div><button className="primary" onClick={onInbox}>Open inbox →</button></div><div className="panel"><h3>A focused operations view</h3><p>Counts reflect conversations you can access. Queued messages have not yet been delivered. Messages with unknown delivery need review before another send.</p></div></>;
}

function Inbox({ user }: { user: User }) {
  const [view, setView] = useState("all"); const [search, setSearch] = useState(""); const [selected, setSelected] = useState<string | null>(null);
  const query = useData<{ items: Conversation[]; total: number }>(user, "inbox:" + view + ":" + search, "/api/conversations?view=" + view + "&q=" + encodeURIComponent(search), true);
  const current = query.data?.items.find(c => c.id === selected);
  useEffect(() => { setSelected(null); }, [view, search]);
  return <><div className="toolbar"><div className="tabs">{["all", "mine", "unassigned", "attention", "waiting", "closed"].map(v => <button key={v} aria-pressed={v === view} className={v === view ? "selected" : ""} onClick={() => setView(v)}>{v.replace("attention", "Needs attention")}</button>)}</div><label className="search"><span className="sr-only">Search conversations</span><input placeholder="Search name or identity…" value={search} onChange={e => setSearch(e.target.value)} /></label></div><ErrorBox error={query.error} /><div className="inbox"><section className="conversation-list" aria-label="Conversations"><div className="list-heading">Conversations <span>{query.data?.total ?? "—"}</span></div>{query.isPending ? <Empty>Loading conversations…</Empty> : !query.data?.items.length ? <Empty>No conversations in this view.</Empty> : query.data.items.map(c => <button className={selected === c.id ? "conversation selected" : "conversation"} key={c.id} onClick={() => setSelected(c.id)}><span className="mini-avatar">{(c.name || c.identity).slice(0, 1).toUpperCase()}</span><div><strong>{c.name || c.identity}</strong><small>{c.channel} · {c.state}</small><span>{c.attention ? "Needs a response" : c.mode === "ai" ? "With Itura" : "With your team"}</span></div>{c.attention && <i className="attention-dot" />}</button>)}</section>{current ? <ConversationPane key={current.id} user={user} conversation={current} /> : <Empty>Select a conversation to see its messages and next steps.</Empty>}</div></>;
}

function ConversationPane({ user, conversation: c }: { user: User; conversation: Conversation }) {
  const messages = useData<Message[]>(user, "messages:" + c.id, "/api/conversations/" + c.id + "/messages", true);
  const notes = useData<Note[]>(user, "notes:" + c.id, "/api/conversations/" + c.id + "/notes");
  const team = useData<User[]>(user, "team", "/api/team");
  const [draft, setDraft] = useState(""); const [note, setNote] = useState(""); const [replyId, setReplyId] = useState(requestId());
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => { bottom.current?.scrollIntoView?.({ block: "end" }); }, [messages.data?.length]);
  const controls = useWrite(user, (change: Partial<Conversation>) => api("/api/conversations/" + c.id, "PATCH", { version: c.version, mode: c.mode, state: c.state, owner_id: c.owner_id, attention: c.attention, ...change }));
  const reply = useWrite(user, (body: { body: string; request_id: string }) => api("/api/conversations/" + c.id + "/reply", "POST", body));
  const addNote = useWrite(user, (body: string) => api("/api/conversations/" + c.id + "/notes", "POST", { body, request_id: requestId() }));
  const readOnly = user.role === "analyst";
  return <><section className="thread"><header className="thread-heading"><div><strong>{c.name || c.identity}</strong><small>{c.channel} · {c.mode === "human" ? "Human support" : "AI assistant"}</small></div><span className="pill">{c.state}</span></header><div className="messages" aria-live="polite"><ErrorBox error={messages.error} />{!messages.data?.length && <Empty>No messages yet.</Empty>}{messages.data?.map(m => <div key={m.id} className={m.direction === "outbound" ? "message outbound" : "message"}><small>{m.sender}</small><div>{m.body}</div><span>{time(m.created_at)}{m.direction === "outbound" ? " · " + m.delivery : ""}</span></div>)}<div ref={bottom} /></div><div className="composer"><ErrorBox error={reply.error} /><textarea aria-label="Reply to customer" placeholder={readOnly ? "You have read-only access" : c.mode === "ai" ? "Take over before replying" : "Write a thoughtful reply…"} disabled={readOnly || c.mode !== "human"} value={draft} onChange={e => { setDraft(e.target.value); setReplyId(requestId()); }} /><div><small>Replies enter the delivery queue.</small><button className="primary" disabled={readOnly || c.mode !== "human" || !draft.trim() || reply.isPending} onClick={() => reply.mutate({ body: draft, request_id: replyId }, { onSuccess: () => { setDraft(""); setReplyId(requestId()); } })}>{reply.isPending ? "Queuing…" : "Send reply →"}</button></div></div></section><aside className="context"><div className="eyebrow">CUSTOMER CONTEXT</div><h3>{c.name || "Customer"}</h3><p className="identity">{c.identity}</p><label>Owner<select disabled={readOnly || controls.isPending || user.role !== "admin"} value={c.owner_id || ""} onChange={e => controls.mutate({ owner_id: e.target.value || null })}><option value="">Unassigned</option>{team.data?.filter(u => u.active && u.role !== "analyst").map(u => <option key={u.id} value={u.id}>{u.name}</option>)}</select></label>{!readOnly && <div className="control-actions"><button disabled={controls.isPending} onClick={() => controls.mutate({ mode: "human", owner_id: user.id })}>Take over</button><button disabled={controls.isPending} onClick={() => { if (confirm("Return this conversation to Itura? Automated replies require approved active AI settings.")) controls.mutate({ mode: "ai", attention: false }); }}>Return to AI</button><button disabled={controls.isPending} onClick={() => controls.mutate({ state: c.state === "closed" ? "open" : "closed", attention: false })}>{c.state === "closed" ? "Reopen" : "Close conversation"}</button></div>}<ErrorBox error={controls.error} /><hr /><h3>Internal notes</h3><ErrorBox error={notes.error || addNote.error} />{notes.data?.map(n => <div className="note" key={n.id}>{n.body}<small>{time(n.created_at)}</small></div>)}{!notes.data?.length && <p>No notes yet.</p>}{!readOnly && <><textarea aria-label="Internal note" placeholder="Add context for your team…" value={note} onChange={e => setNote(e.target.value)} /><button disabled={!note.trim() || addNote.isPending} onClick={() => addNote.mutate(note, { onSuccess: () => setNote("") })}>Add note</button></>}</aside></>;
}

function Customers({ user }: { user: User }) {
  const query = useData<Contact[]>(user, "contacts", "/api/contacts");
  return <div className="panel"><ErrorBox error={query.error} /><table><thead><tr><th>Customer</th><th>Channel identity</th><th>Stage</th><th>Messaging</th></tr></thead><tbody>{query.data?.map(c => <tr key={c.id}><td>{c.name || "Not provided"}</td><td>{c.external_id}</td><td><span className="pill">{c.stage}</span></td><td>{c.opted_out ? "Opted out" : "Active conversation"}</td></tr>)}</tbody></table>{!query.isPending && !query.data?.length && <Empty>Customers appear here when a conversation begins.</Empty>}</div>;
}

function Catalog({ user }: { user: User }) {
  const query = useData<Product[]>(user, "products", "/api/products");
  const initial = { name: "", price: "", currency: "NGN", description: "", active: true };
  const [form, setForm] = useState(initial); const [editing, setEditing] = useState<string | null>(null);
  const save = useWrite(user, () => api("/api/products" + (editing ? "/" + editing : ""), editing ? "PATCH" : "POST", form));
  return <div className="split"><div className="panel"><ErrorBox error={query.error} />{query.data?.map(p => <div className="record" key={p.id}><div><strong>{p.name}</strong><p>{p.description}</p><small>{p.currency} {Number(p.price).toLocaleString(undefined, { minimumFractionDigits: 2 })} · {p.active ? "Active" : "Archived"}</small></div>{user.role === "admin" && <button onClick={() => { setEditing(p.id); setForm({ name: p.name, price: p.price, currency: p.currency, description: p.description, active: p.active }); }}>Edit</button>}</div>)}{!query.isPending && !query.data?.length && <Empty>No products yet. Add approved catalog data to ground recommendations.</Empty>}</div>{user.role === "admin" && <form className="panel form" onSubmit={e => { e.preventDefault(); save.mutate(undefined, { onSuccess: () => { setForm(initial); setEditing(null); } }); }}><h3>{editing ? "Edit product" : "Add a product"}</h3><label>Name<input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label><div className="form-row"><label>Price<input type="number" min="0" step="0.01" required value={form.price} onChange={e => setForm({ ...form, price: e.target.value })} /></label><label>Currency<input maxLength={3} required value={form.currency} onChange={e => setForm({ ...form, currency: e.target.value.toUpperCase() })} /></label></div><label>Description<textarea value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label><label className="check"><input type="checkbox" checked={form.active} onChange={e => setForm({ ...form, active: e.target.checked })} />Available for recommendations</label><ErrorBox error={save.error} /><button className="primary" disabled={save.isPending}>Save product</button>{editing && <button type="button" onClick={() => { setEditing(null); setForm(initial); }}>Cancel editing</button>}</form>}</div>;
}

function Channels({ user }: { user: User }) {
  const query = useData<Channel[]>(user, "channels", "/api/channels");
  const [name, setName] = useState(""); const [provider, setProvider] = useState<"web" | "whatsapp">("web"); const [external, setExternal] = useState("");
  const [origins, setOrigins] = useState(""); const [tokenRef, setTokenRef] = useState(""); const [secretRef, setSecretRef] = useState(""); const [verifyRef, setVerifyRef] = useState(""); const [version, setVersion] = useState("");
  const create = useWrite(user, () => api("/api/channels", "POST", { name, provider, external_id: provider === "whatsapp" ? external : null, enabled: false, config: provider === "web" ? { origins: origins.split(",").map(s => s.trim()).filter(Boolean) } : { token_ref: tokenRef, app_secret_ref: secretRef, verify_token_ref: verifyRef, api_version: version } }));
  const toggle = useWrite(user, (c: Channel) => api("/api/channels/" + c.id, "PATCH", { name: c.name, provider: c.provider, external_id: c.external_id, config: c.config, enabled: !c.enabled }));
  return <div className="split"><div className="panel"><ErrorBox error={query.error || toggle.error} />{query.data?.map(c => <div className="channel-card" key={c.id}><div className="record"><div><strong>{c.name}</strong><small>{c.provider} · {c.enabled ? "Enabled" : "Disabled"}</small></div><button disabled={toggle.isPending} onClick={() => toggle.mutate(c)}>{c.enabled ? "Disable" : "Enable"}</button></div><label>Connection ID<input readOnly value={c.id} /></label>{c.provider === "whatsapp" ? <label>Webhook path<input readOnly value={"/webhooks/whatsapp/" + c.id} /></label> : <p>Use this connection ID in the website widget.</p>}<small>Enabled means this workspace accepts events. It does not confirm provider connectivity.</small></div>)}{!query.data?.length && <Empty>Connect your first channel to begin.</Empty>}</div><form className="panel form" onSubmit={e => { e.preventDefault(); create.mutate(undefined, { onSuccess: () => setName("") }); }}><h3>Add a channel</h3><label>Account name<input value={name} required onChange={e => setName(e.target.value)} /></label><label>Channel<select value={provider} onChange={e => setProvider(e.target.value as "web" | "whatsapp")}><option value="web">Website chat</option><option value="whatsapp">WhatsApp</option></select></label>{provider === "web" ? <label>Allowed website origins<input value={origins} placeholder="https://your-website.com" required onChange={e => setOrigins(e.target.value)} /><small>Exact origins, separated by commas.</small></label> : <><label>Phone-number ID<input required value={external} onChange={e => setExternal(e.target.value)} /></label><label>API credential reference<input required placeholder="ITURA_SECRET_WHATSAPP" value={tokenRef} onChange={e => setTokenRef(e.target.value)} /></label><label>App-secret reference<input required placeholder="ITURA_SECRET_META_APP" value={secretRef} onChange={e => setSecretRef(e.target.value)} /></label><label>Verification-token reference<input required placeholder="ITURA_SECRET_VERIFY" value={verifyRef} onChange={e => setVerifyRef(e.target.value)} /></label><label>Approved Graph API version<input required placeholder="v…" value={version} onChange={e => setVersion(e.target.value)} /></label><p>Credentials are stored on the server. This form binds the account to those protected credentials.</p></>}<ErrorBox error={create.error} /><button disabled={create.isPending} className="primary">Create disabled connection</button></form></div>;
}

function Team({ user }: { user: User }) {
  const query = useData<User[]>(user, "team", "/api/team", true);
  const [name, setName] = useState(""); const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [role, setRole] = useState("agent");
  const create = useWrite(user, () => api("/api/team", "POST", { name, email, password, role }));
  const access = useWrite(user, (u: User) => api("/api/team/" + u.id, "PATCH", { active: !u.active, role: u.role }));
  return <div className="split"><div className="panel"><ErrorBox error={query.error || access.error} />{query.data?.map(u => <div className="record" key={u.id}><div><strong>{u.name}</strong><small>{u.email} · {u.role}</small><span className="pill">{!u.active ? "Suspended" : u.heartbeat_at && Date.now() / 1000 - u.heartbeat_at < 90 ? u.away ? "Away" : "Online" : "Offline"}</span></div>{u.id !== user.id && <button disabled={access.isPending} onClick={() => { if (confirm(`${u.active ? "Suspend" : "Reactivate"} ${u.name}? Existing sessions will be revoked.`)) access.mutate(u); }}>{u.active ? "Suspend" : "Reactivate"}</button>}</div>)}</div><form className="panel form" onSubmit={e => { e.preventDefault(); create.mutate(undefined, { onSuccess: () => { setName(""); setEmail(""); setPassword(""); } }); }}><h3>Add a team member</h3><label>Name<input required value={name} onChange={e => setName(e.target.value)} /></label><label>Email<input type="email" required value={email} onChange={e => setEmail(e.target.value)} /></label><label>Initial password<input type="password" autoComplete="new-password" minLength={12} required value={password} onChange={e => setPassword(e.target.value)} /></label><label>Access<select value={role} onChange={e => setRole(e.target.value)}><option value="agent">Agent · assigned and unassigned conversations</option><option value="analyst">Analyst · read-only workspace data</option><option value="admin">Administrator · workspace management</option></select></label><ErrorBox error={create.error} /><button className="primary" disabled={create.isPending}>Create account</button></form></div>;
}

function AiSettings({ user }: { user: User }) {
  const query = useData<Settings>(user, "settings", "/api/settings");
  return <><ErrorBox error={query.error} />{query.data ? <SettingsEditor key={query.data.version} user={user} initial={query.data} /> : <Empty>Loading approved configuration…</Empty>}</>;
}
function SettingsEditor({ user, initial }: { user: User; initial: Settings }) {
  const [form, setForm] = useState({ ...initial, welcome: initial.welcome ?? "", instruction: initial.instruction ?? "", knowledge: initial.knowledge ?? "", model: initial.model ?? "", ai_secret_ref: initial.ai_secret_ref ?? "" });
  const save = useWrite(user, () => { const { name: _name, ...payload } = form; return api("/api/settings", "PUT", payload); });
  return <form className="panel settings-form" onSubmit={e => { e.preventDefault(); if (confirm("Publish this configuration to the workspace? Automation follows these settings immediately.")) save.mutate(); }}><div className="settings-status"><div><h3>Workspace automation</h3><p>Keep disabled until knowledge, credentials and customer journeys are verified.</p></div><label className="check"><input type="checkbox" checked={form.automation_enabled} onChange={e => setForm({ ...form, automation_enabled: e.target.checked })} />Enabled</label></div><div className="form-row"><label>Model<input value={form.model || ""} onChange={e => setForm({ ...form, model: e.target.value })} /></label><label>AI credential reference<input placeholder="ITURA_SECRET_GROQ" value={form.ai_secret_ref || ""} onChange={e => setForm({ ...form, ai_secret_ref: e.target.value })} /></label></div><label>Welcome message<textarea value={form.welcome || ""} onChange={e => setForm({ ...form, welcome: e.target.value })} /></label><label>Approved instructions<textarea className="long" value={form.instruction || ""} onChange={e => setForm({ ...form, instruction: e.target.value })} /></label><label>Approved knowledge<textarea className="long" value={form.knowledge || ""} onChange={e => setForm({ ...form, knowledge: e.target.value })} /></label><ErrorBox error={save.error} /><div className="toolbar"><small>Configuration version {initial.version}</small><button className="primary" disabled={save.isPending}>Publish configuration</button></div></form>;
}

function AuditPage({ user }: { user: User }) {
  const query = useData<Audit[]>(user, "audit", "/api/audit", true);
  return <div className="panel"><ErrorBox error={query.error} /><table><thead><tr><th>Action</th><th>Actor</th><th>Record</th><th>Time</th></tr></thead><tbody>{query.data?.map(a => <tr key={a.id}><td>{a.action}</td><td>{a.actor_id.slice(0, 8)}</td><td>{a.entity_id.slice(0, 8)}</td><td>{time(a.created_at)}</td></tr>)}</tbody></table>{!query.data?.length && <Empty>No audit entries yet.</Empty>}</div>;
}

// Exported for browser/component verification; production rendering starts in main.tsx.
export { ConversationPane, ErrorBox, Login, Catalog };
