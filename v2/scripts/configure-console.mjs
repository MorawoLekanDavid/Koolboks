import fs from 'node:fs';
const origin = process.env.ITURA_API_ORIGIN;
if (!origin) throw new Error('ITURA_API_ORIGIN is required before deployment');
const parsed = new URL(origin);
if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash || parsed.pathname !== '/') throw new Error('Supply an HTTPS API origin without credentials or a path');
fs.writeFileSync('vercel.json', JSON.stringify({
  buildCommand: 'npm run build', outputDirectory: 'dist',
  rewrites: [{ source: '/api/:path*', destination: parsed.origin + '/api/:path*' },
             { source: '/web/:path*', destination: parsed.origin + '/web/:path*' },
             { source: '/((?!assets/|widget-v2.js|favicon.ico).*)', destination: '/index.html' }],
  headers: [{ source: '/(.*)', headers: [{ key: 'X-Content-Type-Options', value: 'nosniff' }, { key: 'Referrer-Policy', value: 'no-referrer' }] }]
}, null, 2));
console.log('Console routing configured. Run the build and stage the deployment before cutover.');
