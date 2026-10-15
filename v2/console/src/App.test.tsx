import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App, Catalog, ConversationPane, Login } from "./App";
import { api } from "./api";
import type { Conversation, User } from "./types";

vi.mock("./api", async () => { const actual = await vi.importActual<object>("./api"); return { ...actual, api: vi.fn() }; });
const user: User = { id: "user-a", workspace_id: "workspace-a", name: "Ada", email: "ada@test.example", role: "admin", active: true, heartbeat_at: null, away: false };
const conversation: Conversation = { id: "conversation-a", name: "Customer", identity: "2348000000000", channel: "WhatsApp", provider: "whatsapp", channel_id: "channel-a", owner_id: user.id, state: "open", mode: "human", attention: false, version: 2, updated_at: 100 };
function mount(element: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}>{element}</QueryClientProvider>) };
}
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });
describe("operator workflows", () => {
  it("renders model/customer HTML as text", async () => {
    vi.mocked(api).mockImplementation(async path => path.endsWith("/messages") ? [{ id: "m", direction: "inbound", sender: "Customer", body: '<img src=x onerror="alert(1)">', kind: "text", delivery: "received", created_at: 100 }] : []);
    const { container } = mount(<ConversationPane user={user} conversation={conversation} />);
    expect(await screen.findByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
  it("queues a reply against the selected conversation with an idempotency key", async () => {
    vi.mocked(api).mockResolvedValue([]);
    mount(<ConversationPane user={user} conversation={conversation} />);
    fireEvent.change(screen.getByLabelText("Reply to customer"), { target: { value: "Hello customer" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reply →" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/api/conversations/conversation-a/reply", "POST", expect.objectContaining({ body: "Hello customer", request_id: expect.any(String) })));
  });
  it("does not let an analyst compose or take over", () => {
    vi.mocked(api).mockResolvedValue([]);
    mount(<ConversationPane user={{ ...user, role: "analyst" }} conversation={conversation} />);
    expect(screen.getByLabelText("Reply to customer")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Take over" })).not.toBeInTheDocument();
  });
  it("requires human mode before sending", () => {
    vi.mocked(api).mockResolvedValue([]);
    mount(<ConversationPane user={user} conversation={{ ...conversation, mode: "ai" }} />);
    expect(screen.getByLabelText("Reply to customer")).toBeDisabled();
  });
  it("uses optimistic version control for takeover", async () => {
    vi.mocked(api).mockResolvedValue([]);
    mount(<ConversationPane user={user} conversation={conversation} />);
    fireEvent.click(screen.getByRole("button", { name: "Take over" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/api/conversations/conversation-a", "PATCH", expect.objectContaining({ version: 2, owner_id: "user-a", mode: "human" })));
  });
  it("clears the draft when the selected conversation is remounted", async () => {
    vi.mocked(api).mockResolvedValue([]);
    const client = new QueryClient();
    const view = render(<QueryClientProvider client={client}><ConversationPane key="a" user={user} conversation={conversation} /></QueryClientProvider>);
    fireEvent.change(screen.getByLabelText("Reply to customer"), { target: { value: "Private draft for A" } });
    view.rerender(<QueryClientProvider client={client}><ConversationPane key="b" user={user} conversation={{ ...conversation, id: "conversation-b" }} /></QueryClientProvider>);
    expect(screen.getByLabelText("Reply to customer")).toHaveValue("");
  });
  it("does not reuse a previous user's message cache", async () => {
    vi.mocked(api).mockImplementation(async path => path.endsWith("/messages") ? [] : []);
    const { client } = mount(<ConversationPane user={user} conversation={conversation} />);
    client.setQueryData(["workspace-a", "old-user", "messages:conversation-a"], [{ body: "Other account secret" }]);
    await waitFor(() => expect(api).toHaveBeenCalled());
    expect(screen.queryByText("Other account secret")).not.toBeInTheDocument();
  });
  it("edits catalog data without leaking read-only identifiers into the input contract", async () => {
    vi.mocked(api).mockImplementation(async (path, method) => path === "/api/products" && !method || path === "/api/products" && method === "GET" ? [{ id: "p1", name: "Freezer", price: "123.45", currency: "NGN", description: "Approved description", active: true }] : {});
    mount(<Catalog user={user} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Save product" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/api/products/p1", "PATCH", { name: "Freezer", price: "123.45", currency: "NGN", description: "Approved description", image_url: "", product_url: "", active: true }));
  });
  it("signs in without writing auth secrets to localStorage", async () => {
    vi.mocked(api).mockResolvedValue(user); const onLogin = vi.fn(); localStorage.clear();
    mount(<Login onLogin={onLogin} error={null} />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@test.example" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "synthetic-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in →" }));
    await waitFor(() => expect(onLogin).toHaveBeenCalled());
    expect(localStorage.length).toBe(0);
  });
  it("renders a scoped empty overview from actual zero counts", async () => {
    vi.mocked(api).mockImplementation(async path => path === "/api/auth/me" ? user : path === "/api/overview" ? { conversations: 0, attention: 0, unassigned: 0, open: 0, closed: 0, pending_sends: 0, delivery_unknown: 0 } : { ok: true });
    mount(<App />);
    expect(await screen.findByText("Your next action", { exact: false })).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("0").length).toBe(7));
  });
});
