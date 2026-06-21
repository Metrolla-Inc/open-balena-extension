'use strict';
// openBalena Image Maker — minimal web service that drives build-image.sh.
// Binds to localhost only; put it behind your reverse proxy / SSH tunnel for access.
// Config via env (see imagemaker.service / .env.example): IMAGEMAKER_DIR, SERVICE_HOME,
//   OPENBALENA_ROOT_CA, OPENBALENA_DB_CONTAINER, DNS_TLD, PUBLIC_TLD, PORT.
const http = require('http');
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const crypto = require('crypto');

const PORT = parseInt(process.env.PORT || '8090', 10);
const HOST = process.env.HOST || '127.0.0.1';
const DIR = process.env.IMAGEMAKER_DIR || __dirname;
const BUILDS = process.env.IMAGEMAKER_BUILDS || '/var/lib/imagemaker/builds';
const DIST = process.env.IMAGEMAKER_DIST || '/var/lib/imagemaker/dist';
const DB_CONTAINER = process.env.OPENBALENA_DB_CONTAINER || 'open-balena-db-1';
const ENV = {
  ...process.env,
  PATH: '/usr/local/bin:/usr/bin:/bin',
  HOME: process.env.SERVICE_HOME || process.env.HOME,
  BALENARC_NO_ANALYTICS: '1',
  NODE_EXTRA_CA_CERTS: process.env.OPENBALENA_ROOT_CA || '/usr/local/share/ca-certificates/openbalena-root-ca.crt',
};
for (const k of ['DNS_TLD', 'PUBLIC_TLD']) {
  if (!ENV[k]) { console.error(`FATAL: ${k} not set`); process.exit(1); }
}
fs.mkdirSync(BUILDS, { recursive: true });

const jobs = {};
function run(cmd, args, env) {
  return new Promise((res, rej) => execFile(cmd, args, { env: env || ENV, maxBuffer: 32 * 1024 * 1024 },
    (e, so, se) => e ? rej(new Error((se || '') + (e.message || ''))) : res(so)));
}
function send(res, code, type, body) { res.writeHead(code, { 'Content-Type': type }); res.end(body); }
function json(res, code, obj) { send(res, code, 'application/json', JSON.stringify(obj)); }
function sanFleet(f) { return f.replace(/[^a-zA-Z0-9._-]/g, '-'); }
function prebuiltPath(fleet, dt, ver, conn) { return `${DIST}/${sanFleet(fleet)}__${dt}-${ver}__${conn}.img.gz`; }

async function fleets() {
  const out = await run('sudo', ['-n', 'docker', 'exec', DB_CONTAINER, 'psql', '-U', 'docker', '-d', 'resin', '-tAF', '|', '-c',
    'select a.slug, dt.slug from application a join "device type" dt on a."is for-device type"=dt.id order by a.id;']);
  return out.trim().split('\n').filter(Boolean).map(l => { const p = l.split('|'); return { slug: p[0], deviceType: p[1] }; });
}
async function versions(dt) {
  if (!/^[a-z0-9-]+$/.test(dt)) throw new Error('bad device type');
  const out = await run('balena', ['os', 'versions', dt], { ...ENV, BALENARC_BALENA_URL: 'balena-cloud.com' });
  return out.trim().split('\n').map(s => s.trim().replace(/^v/, '')).filter(v => /^[0-9]/.test(v));
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x'); const p = u.pathname;
  if (req.method === 'GET' && p === '/') return fs.readFile(`${DIR}/index.html`, (e, d) => e ? send(res, 500, 'text/plain', 'no ui') : send(res, 200, 'text/html', d));
  if (req.method === 'GET' && p === '/api/fleets') return fleets().then(f => json(res, 200, f)).catch(e => json(res, 500, { error: String(e.message) }));
  if (req.method === 'GET' && p === '/api/versions') return versions(u.searchParams.get('deviceType') || '').then(v => json(res, 200, v)).catch(e => json(res, 500, { error: String(e.message) }));
  if (req.method === 'POST' && p === '/api/build') {
    let b = ''; req.on('data', c => { b += c; if (b.length > 1e6) req.destroy(); });
    req.on('end', () => {
      let q; try { q = JSON.parse(b); } catch (e) { return json(res, 400, { error: 'bad json' }); }
      const fleet = q.fleet, dt = q.deviceType, ver = q.version;
      const net = q.network === 'wifi' ? 'wifi' : 'ethernet';
      const conn = q.connectivity === 'internet' ? 'internet' : 'lan';
      if (!fleet || !dt || !ver) return json(res, 400, { error: 'missing fields' });
      if (!/^[a-z0-9-]+$/.test(dt) || !/^[0-9][0-9a-zA-Z.+-]*$/.test(ver)) return json(res, 400, { error: 'bad params' });
      if (net === 'ethernet') {                       // instant path: serve a prebuilt image if present
        const pre = prebuiltPath(fleet, dt, ver, conn);
        if (fs.existsSync(pre)) {
          const id = crypto.randomBytes(8).toString('hex');
          jobs[id] = { status: 'done', name: `${sanFleet(fleet)}-${dt}-${ver}-${conn}.img.gz`, file: pre, log: '/dev/null', started: Date.now(), prebuilt: true };
          return json(res, 200, { id, prebuilt: true });
        }
      }
      if (Object.values(jobs).filter(j => j.status === 'building').length >= 2) return json(res, 429, { error: 'two builds already running; retry shortly' });
      const id = crypto.randomBytes(8).toString('hex');
      const out = `${BUILDS}/${id}.img.gz`, log = `${BUILDS}/${id}.log`;
      jobs[id] = { status: 'building', name: `${sanFleet(fleet)}-${dt}-${ver}.img.gz`, file: out, log, started: Date.now() };
      const ps = spawn(`${DIR}/build-image.sh`, [dt, ver, fleet, net, q.ssid || '', q.key || '', conn, out, log], { env: ENV });
      ps.on('exit', code => {
        if (code === 0 && fs.existsSync(out)) jobs[id].status = 'done';
        else { jobs[id].status = 'error'; try { jobs[id].error = fs.readFileSync(log, 'utf8').split('\n').slice(-6).join(' '); } catch (e) { jobs[id].error = 'build failed'; } }
      });
      ps.on('error', e => { jobs[id].status = 'error'; jobs[id].error = e.message; });
      return json(res, 200, { id });
    });
    return;
  }
  if (req.method === 'GET' && p.startsWith('/api/status/')) {
    const j = jobs[p.split('/').pop()]; if (!j) return json(res, 404, { error: 'no such job' });
    let last = ''; try { const t = fs.readFileSync(j.log, 'utf8').trim().split('\n'); last = t[t.length - 1] || ''; } catch (e) {}
    if (j.prebuilt) last = 'prebuilt image ready';
    return json(res, 200, { status: j.status, message: last, error: j.error || null, name: j.name });
  }
  if (req.method === 'GET' && p.startsWith('/download/')) {
    const j = jobs[p.split('/').pop()];
    if (!j || j.status !== 'done' || !fs.existsSync(j.file)) return send(res, 404, 'text/plain', 'not ready');
    res.writeHead(200, { 'Content-Type': 'application/gzip', 'Content-Disposition': `attachment; filename="${j.name}"`, 'Content-Length': fs.statSync(j.file).size });
    return fs.createReadStream(j.file).pipe(res);
  }
  send(res, 404, 'text/plain', 'not found');
});
server.listen(PORT, HOST, () => console.log(`imagemaker listening on ${HOST}:${PORT}`));
