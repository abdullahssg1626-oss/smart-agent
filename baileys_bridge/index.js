const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const pino = require('pino');
const qrcode = require('qrcode-terminal');
const baileys = require('@whiskeysockets/baileys');

const makeWASocket = baileys.default;
const { DisconnectReason, useMultiFileAuthState } = baileys;
const logger = pino({ level: process.env.LOG_LEVEL || 'info' });
const token = process.env.WHATSAPP_BRIDGE_TOKEN || '';
const djangoWebhookSetting = process.env.DJANGO_WEBHOOK_URL || '';
const djangoWebhookUrl = djangoWebhookSetting
  ? new URL(
    /^https?:\/\//i.test(djangoWebhookSetting)
      ? djangoWebhookSetting
      : `https://${djangoWebhookSetting}`,
  )
  : null;
const authDirectory = path.resolve(__dirname, process.env.AUTH_DIRECTORY || 'auth_info');
const port = Number(process.env.PORT || 3001);
const host = process.env.HOST || '0.0.0.0';
const maxBodyBytes = 16 * 1024;
let socket;
let connected = false;
let reconnectTimer;

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
  if (/^\d{5,15}$/.test(recipient)) return `${recipient}@s.whatsapp.net`;
  if (/^\d{5,15}@s\.whatsapp\.net$/.test(recipient)) return recipient;
  if (/^\d{5,27}@lid$/.test(recipient)) return recipient;
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
  if (!connected || !socket) {
    return sendJson(response, 503, { error: 'Baileys is not connected to WhatsApp.' });
  }

  try {
    const result = await socket.sendMessage(recipient, {
      text: payload.message.slice(0, 4096),
    });
    return sendJson(response, 200, { id: result.key.id });
  } catch (error) {
    logger.error({ err: error }, 'Could not send WhatsApp message.');
    return sendJson(response, 502, { error: 'Could not send WhatsApp message.' });
  }
});

async function forwardIncomingMessage(message) {
  const remoteJid = message.key?.remoteJid;
  if (
    message.key?.fromMe
    || !message.key?.id
    || !remoteJid
    || remoteJid === 'status@broadcast'
    || remoteJid.endsWith('@g.us')
  ) return;

  const content = message.message;
  const text = content?.conversation
    || content?.extendedTextMessage?.text
    || content?.imageMessage?.caption
    || content?.videoMessage?.caption;
  if (typeof text !== 'string' || !text.trim()) return;

  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(djangoWebhookUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ id: message.key.id, from: remoteJid, text: text.trim() }),
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

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = undefined;
    connect().catch((error) => {
      logger.error({ err: error }, 'Baileys reconnect failed.');
      scheduleReconnect();
    });
  }, 3000);
}

async function connect() {
  fs.mkdirSync(authDirectory, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(authDirectory);
  socket = makeWASocket({
    auth: state,
    logger: pino({ level: 'silent' }),
    printQRInTerminal: false,
  });
  socket.ev.on('creds.update', saveCreds);
  socket.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) qrcode.generate(qr, { small: true });
    if (connection === 'open') {
      connected = true;
      logger.info('Baileys connected to WhatsApp.');
    } else if (connection === 'close') {
      connected = false;
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      logger.warn({ statusCode }, 'Baileys connection closed.');
      if (statusCode !== DisconnectReason.loggedOut) {
        scheduleReconnect();
      } else {
        logger.error('WhatsApp session logged out; remove the auth directory and scan a new QR code.');
      }
    }
  });
  socket.ev.on('messages.upsert', ({ type, messages }) => {
    if (type !== 'notify') return;
    for (const message of messages) {
      forwardIncomingMessage(message).catch((error) => {
        logger.error({ err: error }, 'Could not forward incoming WhatsApp message to Django.');
      });
    }
  });
}

server.listen(port, host, () => {
  logger.info({ host, port }, 'Baileys bridge HTTP server listening.');
  connect().catch((error) => {
    logger.error({ err: error }, 'Baileys startup failed.');
    scheduleReconnect();
  });
});

function shutdown() {
  connected = false;
  clearTimeout(reconnectTimer);
  server.close(() => process.exit(0));
  if (socket) socket.end(undefined);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
