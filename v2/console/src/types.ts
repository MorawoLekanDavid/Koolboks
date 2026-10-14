export interface User { id: string; workspace_id: string; name: string; email: string; role: "admin" | "agent" | "analyst"; active: boolean; heartbeat_at: number | null; away: boolean; }
export interface Conversation { id: string; name: string; identity: string; channel: string; provider: string; channel_id: string; owner_id: string | null; state: string; mode: string; attention: boolean; version: number; updated_at: number; }
export interface Message { id: string; direction: string; sender: string; body: string; kind: string; delivery: string; created_at: number; }
export interface Note { id: string; body: string; created_at: number; }
export interface Product { id: string; name: string; price: string; currency: string; description: string; active: boolean; }
export interface Channel { id: string; name: string; provider: "web" | "whatsapp"; external_id: string | null; enabled: boolean; config: Record<string, unknown>; }
export interface Contact { id: string; name: string; external_id: string; stage: string; opted_out: boolean; }
export interface Settings { name: string; version: number; automation_enabled: boolean; welcome: string; instruction: string; knowledge: string; model: string; ai_secret_ref: string; }
export interface Audit { id: string; action: string; actor_id: string; entity_id: string; created_at: number; }
