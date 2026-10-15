import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import source from '../public/widget-v2.js?raw';

beforeEach(() => { document.body.replaceChildren(); sessionStorage.clear(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function launch(scriptOrigin = window.location.origin) {
  const script = document.createElement('script');
  script.src = scriptOrigin + '/widget-v2.js';
  script.dataset.channel = 'synthetic-channel';
  vi.spyOn(document, 'currentScript', 'get').mockReturnValue(script);
  Function(source)();
  return document.body.lastElementChild!.shadowRoot!;
}

test('protected same-origin test page keeps preview authentication and renders replies safely', async () => {
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ token: 'synthetic-visitor' }) })
    .mockResolvedValue({ ok: true, json: async () => [{id:'one',direction:'outbound',body:'<img src=x onerror=alert(1)>'}] });
  vi.stubGlobal('fetch', fetch);
  const root = launch();
  (root.querySelector('.bubble') as HTMLButtonElement).click();
  await vi.waitFor(() => expect(root.querySelector('.message')?.textContent).toBe('<img src=x onerror=alert(1)>'));
  expect(root.querySelector('.message img')).toBeNull();
  expect(fetch.mock.calls[0][1].credentials).toBe('same-origin');
  expect(fetch.mock.calls[1][1].headers.Authorization).toBe('Bearer synthetic-visitor');
  (root.querySelector('.close') as HTMLButtonElement).click();
});

test('external website embeds omit cookies and show a clear error for an HTML response', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => { throw new SyntaxError('Unexpected token'); } });
  vi.stubGlobal('fetch', fetch);
  const root = launch('https://synthetic-chat.example');
  (root.querySelector('.bubble') as HTMLButtonElement).click();
  await vi.waitFor(() => expect(root.querySelector('.error')?.textContent).toContain('Chat is temporarily unavailable'));
  expect(fetch.mock.calls[0][1].credentials).toBe('omit');
});

test('product photo uses the visitor token and a protected message URL', async () => {
  const fetch = vi.fn().mockResolvedValueOnce({ok:true,json:async()=>({token:'synthetic-visitor'})})
    .mockResolvedValueOnce({ok:true,json:async()=>[{id:'photo-one',direction:'outbound',body:'Approved product',kind:'image',media:{name:'Solar freezer',product_url:'javascript:alert(1)'}}]})
    .mockResolvedValueOnce({ok:true,blob:async()=>new Blob(['synthetic'],{type:'image/png'})});
  vi.stubGlobal('fetch',fetch);
  vi.stubGlobal('URL', class extends URL { static createObjectURL(){return 'blob:synthetic';} static revokeObjectURL(){} });
  const root=launch(); (root.querySelector('.bubble') as HTMLButtonElement).click();
  await vi.waitFor(()=>expect(root.querySelector('img')?.getAttribute('alt')).toBe('Solar freezer'));
  expect(fetch.mock.calls[2][0]).toContain('/web/messages/photo-one/image');
  expect(fetch.mock.calls[2][1].headers.Authorization).toBe('Bearer synthetic-visitor');
  expect(root.querySelector('a')).toBeNull();
  (root.querySelector('.close') as HTMLButtonElement).click();
});

test('photo failure preserves the product caption and shows recovery text', async () => {
  const fetch=vi.fn().mockResolvedValueOnce({ok:true,json:async()=>({token:'synthetic-visitor'})})
    .mockResolvedValueOnce({ok:true,json:async()=>[{id:'photo-two',direction:'outbound',body:'Approved caption',kind:'image'}]})
    .mockResolvedValueOnce({ok:false,status:422});
  vi.stubGlobal('fetch',fetch);
  const root=launch(); (root.querySelector('.bubble') as HTMLButtonElement).click();
  await vi.waitFor(()=>expect(root.querySelector('.message')?.textContent).toContain('Photo unavailable'));
  expect(root.querySelector('.message')?.textContent).toContain('Approved caption');
  (root.querySelector('.close') as HTMLButtonElement).click();
});
