// Private browser for reading Garant. Credentials stay in the mounted profile.
const fs = require('fs');
const http = require('http');
const net = require('net');
const { spawn } = require('child_process');

const port = 19222;
const socket = '/run/p10-garant/browser.sock';

if (process.argv[2] === '--healthcheck') {
  const request = http.get(`http://127.0.0.1:${port}/json/version`, response => {
    response.resume();
    process.exit(response.statusCode === 200 ? 0 : 1);
  });
  request.setTimeout(3000, () => request.destroy());
  request.on('error', () => process.exit(1));
  return;
}

process.umask(0o077);
try { fs.unlinkSync(socket); } catch (error) { if (error.code !== 'ENOENT') throw error; }

const browser = spawn('/home/node/.cache/ms-playwright/chromium-1243/chrome-linux-arm64/chrome', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
  '--remote-debugging-address=127.0.0.1', `--remote-debugging-port=${port}`,
  '--remote-allow-origins=devtools://devtools',
  '--user-data-dir=/home/node/garant-profile', '--no-first-run', '--no-default-browser-check',
  'https://internet.garant.ru/',
], { stdio: 'ignore' });

const relay = net.createServer(client => {
  const upstream = net.connect(port, '127.0.0.1');
  client.on('error', () => upstream.destroy());
  upstream.on('error', () => client.destroy());
  client.pipe(upstream).pipe(client);
});
relay.on('error', () => { browser.kill('SIGTERM'); process.exitCode = 1; });
relay.listen(socket, () => fs.chmodSync(socket, 0o600));

browser.on('error', () => { relay.close(); process.exit(1); });
browser.on('exit', code => { relay.close(); process.exit(code === 0 ? 0 : 1); });
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => browser.kill('SIGTERM'));
}
