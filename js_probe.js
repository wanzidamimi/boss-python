const fs = require('fs');
const path = require('path');
const vm = require('vm');

const scriptPath = path.join(__dirname, 'js', 'b9a070d3.js');
const code = fs.readFileSync(scriptPath, 'utf8');

function fakeElement(tag = 'div') {
  const el = {
    tagName: tag.toUpperCase(),
    style: {},
    children: [],
    childNodes: [],
    parentNode: null,
    ownerDocument: null,
    nodeType: 1,
    attributes: {},
    innerHTML: '',
    textContent: '',
    className: '',
    id: '',
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] ?? null; },
    removeAttribute(name) { delete this.attributes[name]; },
    appendChild(child) { this.children.push(child); this.childNodes.push(child); child.parentNode = this; return child; },
    removeChild(child) {
      const i = this.children.indexOf(child);
      if (i >= 0) this.children.splice(i, 1);
      const j = this.childNodes.indexOf(child);
      if (j >= 0) this.childNodes.splice(j, 1);
      child.parentNode = null;
      return child;
    },
    insertBefore(child) { return this.appendChild(child); },
    addEventListener() {},
    removeEventListener() {},
    getElementsByTagName() { return []; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {}, blur() {}, click() {},
    getContext() { return null; },
  };

  if (tag === 'canvas') {
    const { createCanvas } = require('@napi-rs/canvas');
    const canvas = createCanvas(300, 150);
    Object.defineProperty(el, 'width', {
      get() { return canvas.width; },
      set(v) { canvas.width = v; },
    });
    Object.defineProperty(el, 'height', {
      get() { return canvas.height; },
      set(v) { canvas.height = v; },
    });
    el.getContext = (type) => (type === '2d' ? canvas.getContext('2d') : null);
    el.toDataURL = (...args) => (
      canvasUrlQueue.length
        ? canvasUrlQueue.shift()
        : canvas.toDataURL(...args)
    );
  }

  if (tag === 'a') {
    el.href = '';
    el.rel = '';
    el.protocol = 'https:';
    el.host = 'www.zhipin.com';
    el.pathname = '/';
    el.origin = 'https://www.zhipin.com';
    el.relList = {
      value: '',
      supports() { return true; },
      add() {},
      remove() {},
      contains() { return false; },
      toggle() { return false; },
      length: 0,
    };
  }

  if (tag === 'iframe') {
    const iframeDocument = typeof sandbox !== 'undefined' && sandbox.document
      ? sandbox.document
      : null;
    el.contentDocument = iframeDocument;
    el.contentWindow = iframeDocument
      ? {
          document: iframeDocument,
          navigator: sandbox.navigator,
          screen: sandbox.screen,
          location: sandbox.location,
          innerWidth: sandbox.innerWidth,
          innerHeight: sandbox.innerHeight,
          outerWidth: sandbox.outerWidth,
          outerHeight: sandbox.outerHeight,
          devicePixelRatio: sandbox.devicePixelRatio,
          Date,
          Math,
          JSON,
          setTimeout,
          clearTimeout,
        }
      : {};
    el.src = '';
  }

  if (typeof HTMLElement !== 'undefined') {
    if (tag === 'canvas') Object.setPrototypeOf(el, HTMLCanvasElement.prototype);
    else if (tag === 'iframe') Object.setPrototypeOf(el, HTMLIFrameElement.prototype);
    else if (tag === 'a') Object.setPrototypeOf(el, HTMLAnchorElement.prototype);
    else Object.setPrototypeOf(el, HTMLElement.prototype);
  }
  return el;
}

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  Math,
  Date,
  JSON,
  String,
  Number,
  Boolean,
  Array,
  Object,
  RegExp,
  Error,
  TypeError,
  RangeError,
  Function,
  parseInt,
  parseFloat,
  isNaN,
  encodeURIComponent,
  decodeURIComponent,
  escape,
  unescape,
  btoa: (input) => Buffer.from(String(input), 'latin1').toString('base64'),
  atob: (input) => Buffer.from(String(input), 'base64').toString('latin1'),
};

sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.global = sandbox;
sandbox.globalThis = sandbox;

sandbox.navigator = {
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36',
  platform: 'Win32',
  language: 'zh-CN',
  languages: ['zh-CN', 'zh'],
  appName: 'Netscape',
  appVersion: '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
  vendor: 'Google Inc.',
  plugins: [],
  mimeTypes: [],
  cookieEnabled: true,
  doNotTrack: null,
  hardwareConcurrency: 8,
  webdriver: false,
};

const docEl = fakeElement('html');
const headEl = fakeElement('head');
const bodyEl = fakeElement('body');
docEl.ownerDocument = null;
headEl.ownerDocument = null;
bodyEl.ownerDocument = null;

sandbox.document = {
  documentElement: docEl,
  head: headEl,
  body: bodyEl,
  readyState: 'complete',
  title: '',
  URL: 'https://www.zhipin.com/web/geek/jobs',
  referrer: 'https://www.zhipin.com/',
  cookie: '',
  characterSet: 'UTF-8',
  contentType: 'text/html',
  createElement(tag) { return fakeElement(tag); },
  createTextNode(text) { return { nodeType: 3, textContent: text }; },
  createDocumentFragment() { return fakeElement('fragment'); },
  getElementsByTagName(name) {
    if (name === 'head') return [headEl];
    if (name === 'body') return [bodyEl];
    if (name === 'html') return [docEl];
    return [];
  },
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  removeEventListener() {},
  appendChild(child) { return bodyEl.appendChild(child); },
  write() {}, writeln() {},
};

sandbox.location = {
  href: 'https://www.zhipin.com/web/geek/jobs',
  protocol: 'https:',
  host: 'www.zhipin.com',
  hostname: 'www.zhipin.com',
  port: '',
  pathname: '/web/geek/jobs',
  search: '',
  hash: '',
  origin: 'https://www.zhipin.com',
};

sandbox.screen = {
  width: 1920, height: 1080, availWidth: 1920, availHeight: 1080,
  colorDepth: 24, pixelDepth: 24,
};

sandbox.history = { length: 1, back() {}, forward() {}, go() {}, pushState() {}, replaceState() {} };
sandbox.XMLHttpRequest = function XMLHttpRequest() {};
sandbox.Image = function Image() {};
sandbox.Audio = function Audio() {};
sandbox.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {}, clear() {} };
sandbox.sessionStorage = sandbox.localStorage;

function proxyLog(obj, name) {
  return new Proxy(obj, {
    get(target, prop) {
      console.log(`[get] ${name}.${String(prop)}`);
      const value = Reflect.get(target, prop);
      if (typeof value === 'function') {
        return function (...args) {
          console.log(`[call] ${name}.${String(prop)}(${args.map((a) => { try { return JSON.stringify(a); } catch (_) { return String(a); } }).join(',')})`);
          return Reflect.apply(value, target, args);
        };
      }
      return value;
    },
    set(target, prop, value) {
      console.log(`[set] ${name}.${String(prop)}`);
      return Reflect.set(target, prop, value);
    },
  });
}

const browserTokenPath = path.join(__dirname, 'data', 'browser_stoken.json');
const fingerprintPath = path.join(__dirname, 'data', 'browser_fingerprint.json');
const browserTokenData = fs.existsSync(browserTokenPath)
  ? JSON.parse(fs.readFileSync(browserTokenPath, 'utf8'))
  : null;
const fingerprint = JSON.parse(fs.readFileSync(fingerprintPath, 'utf8'));
const fp = browserTokenData || fingerprint.iframe || fingerprint;

if (fp.navigator) {
  const nav = Object.assign({}, fp.navigator);
  if (nav.pluginsLength !== undefined) nav.plugins = new Array(nav.pluginsLength).fill(null);
  if (nav.mimeTypesLength !== undefined) nav.mimeTypes = new Array(nav.mimeTypesLength).fill(null);
  if (nav.webkitTemporaryStorageType === 'object') {
    nav.webkitTemporaryStorage = { queryUsageAndQuota(cb) { cb(0, 0); } };
  }
  Object.assign(sandbox.navigator, nav);
  if (sandbox.navigator.plugins) Object.setPrototypeOf(sandbox.navigator.plugins, PluginArray.prototype);
  if (sandbox.navigator.mimeTypes) Object.setPrototypeOf(sandbox.navigator.mimeTypes, MimeTypeArray.prototype);
  if (sandbox.navigator.webkitTemporaryStorage) {
    Object.setPrototypeOf(sandbox.navigator.webkitTemporaryStorage, StorageInfo.prototype);
  }
  sandbox.PluginArray = PluginArray;
  sandbox.MimeTypeArray = MimeTypeArray;
  sandbox.StorageInfo = StorageInfo;
}
if (fp.screen) Object.assign(sandbox.screen, fp.screen);
if (fp.document) Object.assign(sandbox.document, fp.document);
if (fp.documentExtra) {
  Object.assign(sandbox.document, fp.documentExtra);
  if (fp.documentExtra.cookies !== undefined) sandbox.document.cookie = fp.documentExtra.cookies;
}
if (fp.windowFingerprint) Object.assign(sandbox, fp.windowFingerprint);
if (fp.timezoneOffset !== undefined) sandbox.timezoneOffset = fp.timezoneOffset;
const canvasUrlQueue = [...(fp.canvasUrls || [])];

const envTrace = [];
function traceProxy(obj, name) {
  return new Proxy(obj, {
    get(target, prop) {
      envTrace.push(name + String(prop));
      return Reflect.get(target, prop);
    },
  });
}
function Navigator() {}
function Screen() {}
function Document() {}
function HTMLElement() {}
function HTMLCanvasElement() {}
function HTMLIFrameElement() {}
function HTMLAnchorElement() {}
function PluginArray() {}
function MimeTypeArray() {}
function StorageInfo() {}

Object.setPrototypeOf(sandbox.navigator, Navigator.prototype);
Object.setPrototypeOf(sandbox.screen, Screen.prototype);
Object.setPrototypeOf(sandbox.document, Document.prototype);
Object.setPrototypeOf(sandbox.document.createElement('div'), HTMLElement.prototype);
Object.setPrototypeOf(sandbox.document.createElement('canvas'), HTMLCanvasElement.prototype);
Object.setPrototypeOf(sandbox.document.createElement('iframe'), HTMLIFrameElement.prototype);
Object.setPrototypeOf(sandbox.document.createElement('a'), HTMLAnchorElement.prototype);

sandbox.Navigator = Navigator;
sandbox.Screen = Screen;
sandbox.Document = Document;
sandbox.HTMLElement = HTMLElement;
sandbox.HTMLCanvasElement = HTMLCanvasElement;
sandbox.HTMLIFrameElement = HTMLIFrameElement;
sandbox.HTMLAnchorElement = HTMLAnchorElement;

sandbox.JSON = Object.assign({}, JSON);
const stringifyInputs = [];
const origStringify = sandbox.JSON.stringify;
sandbox.JSON.stringify = function () {
  stringifyInputs.push(arguments[0]);
  return origStringify.apply(sandbox.JSON, arguments);
};

sandbox.navigator = traceProxy(sandbox.navigator, 'navigator.');
sandbox.screen = traceProxy(sandbox.screen, 'screen.');
sandbox.document = traceProxy(sandbox.document, 'document.');

const origCreateElement = sandbox.document.createElement;
sandbox.document.createElement = function (tag) {
  envTrace.push('createElement:' + String(tag));
  return origCreateElement.call(sandbox.document, tag);
};

vm.createContext(sandbox);

try {
  vm.runInContext(code, sandbox, { filename: 'b9a070d3.js' });
  console.log('[OK] script executed');
} catch (err) {
  console.error('[RUN ERROR]', err && err.stack ? err.stack : err);
}

console.log('[check] sandbox.ABC =', typeof sandbox.ABC);
console.log('[check] window.ABC =', typeof sandbox.window && typeof sandbox.window.ABC);
console.log('[check] md5 =', typeof sandbox.md5);

if (sandbox.ABC) {
  console.log('[ABC keys]', Object.getOwnPropertyNames(sandbox.ABC));
  for (const key of Object.getOwnPropertyNames(sandbox.ABC)) {
    console.log(`  ABC.${key}:`, typeof sandbox.ABC[key]);
  }
  console.log('[ABC.prototype keys]', Object.getOwnPropertyNames(sandbox.ABC.prototype));
  for (const key of Object.getOwnPropertyNames(sandbox.ABC.prototype)) {
    console.log(`  ABC.prototype.${key}:`, typeof sandbox.ABC.prototype[key]);
  }

  const paramsPath = path.join(__dirname, 'data', 'test_params.json');
  const browserPath = path.join(__dirname, 'data', 'browser_stoken.json');
  const cookiePath = path.join(__dirname, 'data', 'cookies_logged.json');

  let seed;
  let sts;
  let saved = '';

  if (fs.existsSync(paramsPath)) {
    const params = JSON.parse(fs.readFileSync(paramsPath, 'utf8'));
    seed = params.seed;
    sts = parseInt(params.sts, 10);
    if (fs.existsSync(browserPath)) {
      saved = JSON.parse(fs.readFileSync(browserPath, 'utf8')).token || '';
    }
  } else if (fs.existsSync(cookiePath)) {
    const cookieData = JSON.parse(fs.readFileSync(cookiePath, 'utf8'));
    const cookies = Object.fromEntries(
      (cookieData.cookies || []).map((item) => [item.name, item.value])
    );
    seed = cookies.__zp_sseed__;
    sts = parseInt(cookies.__zp_sts__, 10);
    saved = cookies.__zp_stoken__ || '';
  }

  if (seed && Number.isFinite(sts)) {
    const tzOffset = fs.existsSync(paramsPath)
      ? JSON.parse(fs.readFileSync(paramsPath, 'utf8')).timezoneOffset
      : (fp.timezoneOffset !== undefined ? fp.timezoneOffset : new Date().getTimezoneOffset());
    const timezoneAdjust = 60 * (480 + tzOffset) * 1000;
    fs.writeFileSync(
      path.join(__dirname, 'data', 'node_trace_full.json'),
      JSON.stringify(envTrace, null, 2)
    );
    envTrace.length = 0;
    stringifyInputs.length = 0;
    const generated = new sandbox.ABC().z(seed, sts + timezoneAdjust);
    let savedRaw = saved;
    try { savedRaw = decodeURIComponent(saved); } catch (_) {}

    console.log('\n[generate] canvasUrlQueue remaining =', canvasUrlQueue.length);
    console.log('[generate] seed =', seed);
    console.log('[generate] sts =', sts, 'timezoneAdjust =', timezoneAdjust);
    console.log('[generate] token length =', generated.length, 'saved length =', savedRaw.length);
    console.log('[generate] raw match =', generated === savedRaw);
    console.log('[generate] encoded match =', encodeURIComponent(generated) === saved);
    console.log('[generate] generated prefix =', generated.slice(0, 80));
    console.log('[generate] browser prefix   =', savedRaw.slice(0, 80));
    fs.writeFileSync(
      path.join(__dirname, 'data', 'generated_stoken.json'),
      JSON.stringify({ seed, sts, timezoneAdjust, token: generated, browserToken: saved }, null, 2)
    );
    fs.writeFileSync(
      path.join(__dirname, 'data', 'node_trace.json'),
      JSON.stringify(envTrace, null, 2)
    );
    fs.writeFileSync(
      path.join(__dirname, 'data', 'node_stringify_inputs.json'),
      JSON.stringify(stringifyInputs.map((x) => {
        try { return origStringify(x); } catch (_) { return String(x); }
      }), null, 2)
    );
  }
}
