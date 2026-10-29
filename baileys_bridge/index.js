const http = require('node:http');
const crypto = require('node:crypto');
const { create } = require('@open-wa/wa-automate');

const token = process.env.WHATSAPP_BRIDGE_TOKEN || '';
const djangoWebhookSetting = process.env.DJANGO_WEBHOOK_URL || '';
const djangoWebhookUrl = djangoWebhookSetting
  ? new URL(
      /^https?:\/\//i.test(djangoWebhookSetting)
        ? djangoWebhookSetting
        : `https://${djangoWebhookSetting}`,
    )
  : null;
const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || '0.0.0.0';
const maxBodyBytes = 16 * 1024;

let waClient = null;
let connected = false;

if (!token || !djangoWebhookUrl || !Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('Set WHATSAPP_BRIDGE_TOKEN, DJANGO_WEBHOOK_URL, and a valid PORT.');
}
if (djangoWebhookUrl.pathname === '/') {
  djangoWebhookUrl.pathname = '/webhook/baileys/';
}

function isAuthorized(request) {
  const authorization = request.headers.authorization || '';
  const supplied = authorization.startsWith('Bearer ') ? authorization.slice(7) : '';
  const expectedBuffer = Buffer.from(token);
  const suppliedBuffer = Buffer.from(supplied);
  return suppliedBuffer.length === expectedBuffer.length
    && crypto.timingSafeEqual(suppliedBuffer, expectedBuffer);
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
      if (Buffer.byteLength(body, 'utf8') > maxBodyBytes) {
        reject(new Error('Request body is too large.'));
        request.destroy();
      }
    });
    request.on('end', () => {
      try {
        resolve(JSON.parse(body));
      } catch {
        reject(new Error('Request body must be valid JSON.'));
      }
    });
    request.on('error', reject);
  });
}

function sendJson(response, status, payload) {
  response.writeHead(status, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify(payload));
}

function normalizeRecipient(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const recipient = value.trim();
  if (/^\d{5,15}$/.test(recipient)) return `${recipient}@c.us`;
  if (/^\d{5,15}@c\.us$/.test(recipient)) return recipient;
  return null;
}

const server = http.createServer(async (request, response) => {
  if (request.method === 'GET' && request.url === '/health') {
    return sendJson(response, 200, { status: connected ? 'connected' : 'disconnected' });
  }

  if (!isAuthorized(request)) {
    return sendJson(response, 401, { error: 'Unauthorized.' });
  }

  if (request.method !== 'POST' || request.url !== '/messages') {
    return sendJson(response, 404, { error: 'Not found.' });
  }

  let payload;
  try {
    payload = await readJson(request);
  } catch (error) {
    return sendJson(response, 400, { error: error.message });
  }

  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return sendJson(response, 400, { error: 'Request body must be a JSON object.' });
  }
  const recipient = normalizeRecipient(payload.to);
  if (!recipient || typeof payload.message !== 'string' || !payload.message.trim()) {
    return sendJson(response, 400, { error: 'A valid individual recipient and message are required.' });
  }
  if (!connected || !waClient) {
    return sendJson(response, 503, { error: 'OpenWA is not connected to WhatsApp.' });
  }

  try {
    const result = await waClient.sendText(recipient, payload.message.slice(0, 4096));
    return sendJson(response, 200, { id: result });
  } catch (error) {
    console.error('Could not send WhatsApp message:', error);
    return sendJson(response, 502, { error: 'Could not send WhatsApp message.' });
  }
});

async function forwardIncomingMessage(message) {
  if (message.fromMe || message.isGroupMsg || !message.chatId) return;
  const text = message.body || message.caption;
  if (typeof text !== 'string' || !text.trim()) return;

  // Format JID to match Django backend expectations (@s.whatsapp.net)
  const remoteJid = message.from.includes('@c.us') 
    ? message.from.replace('@c.us', '@s.whatsapp.net') 
    : `${message.from.replace(/@.*/, '')}@s.whatsapp.net`;

  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(djangoWebhookUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ 
          id: message.id, 
          from: remoteJid, 
          text: text.trim() 
        }),
        signal: AbortSignal.timeout(15000),
      });
      if (response.ok) return;
      const responseText = await response.text();
      throw new Error(`Django webhook returned HTTP ${response.status}: ${responseText}`);
    } catch (error) {
      if (attempt === 2) throw error;
      await new Promise((resolve) => setTimeout(resolve, 500 * (2 ** attempt)));
    }
  }
}

create({
  sessionId: 'smart-agent',
  multiDevice: true,
  authTimeout: 60,
  blockCrashLogs: true,
  qrTimeout: 0,
  cacheEnabled: false,
}).then(async (client) => {
  waClient = client;
  connected = true;
  console.log('OpenWA connected to WhatsApp successfully.');

  client.onMessage(async (message) => {
    try {
      await forwardIncomingMessage(message);
    } catch (error) {
      console.error('Could not forward incoming WhatsApp message to Django:', error);
    }
  });
}).catch((error) => {
  console.error('OpenWA initialization failed:', error);
});

server.listen(port, host, () => {
  console.log(`OpenWA bridge HTTP server listening on ${host}:${port}`);
});