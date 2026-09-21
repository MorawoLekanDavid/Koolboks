/* Koolbuy / Itura chat widget loader.
 * Embed on any site with one line, right before </body>:
 *   <script src="https://chat.koolbuystore.com/widget.js" async></script>
 * Creates a fixed-position iframe (closed/bubble by default) and resizes it
 * between bubble / minimized / open based on postMessage events sent by the
 * chat page itself (see notifyParent() in index.html) — that's what stops a
 * permanently large, invisible iframe from blocking clicks on the rest of
 * the host page while the chat is closed.
 */
(function () {
    var ORIGIN = 'https://chat.koolbuystore.com';
    var SIZES = {
        closed: { w: '84px', h: '84px' },
        minimized: { w: 'min(420px, 100vw)', h: '76px' },
        open: { w: 'min(420px, 100vw)', h: 'min(650px, 100vh)' }
    };

    var iframe = document.createElement('iframe');
    iframe.src = ORIGIN + '/?start=closed';
    iframe.title = 'Chat with Koolbuy';
    iframe.setAttribute('aria-label', 'Chat with Koolbuy');
    iframe.style.cssText = [
        'position:fixed', 'bottom:0', 'right:0', 'border:0',
        'z-index:2147483000', 'background:transparent',
        'width:' + SIZES.closed.w, 'height:' + SIZES.closed.h,
        'max-width:100vw', 'max-height:100vh',
        'colorScheme:light'
    ].join(';');

    function mount() {
        document.body.appendChild(iframe);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }

    window.addEventListener('message', function (e) {
        if (e.origin !== ORIGIN) return;
        if (!e.data || e.data.source !== 'koolbuy-chat') return;
        var size = SIZES[e.data.state] || SIZES.closed;
        iframe.style.width = size.w;
        iframe.style.height = size.h;
    });
})();
