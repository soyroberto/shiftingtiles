const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const ROOT = path.resolve(__dirname, '..');

function read(relativePath) {
  return fs.readFileSync(path.join(ROOT, relativePath), 'utf8');
}

async function compiledRules() {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {runScripts: 'outside-only'});
  const {window} = dom;
  const compilerScript = window.document.createElement('script');
  Object.defineProperty(window.document, 'currentScript', {configurable: true, get: () => compilerScript});
  window.eval(read('less-2.5.1.min.js'));
  const css = await new Promise((resolve, reject) => {
    window.less.render(read('shiftingtiles.less'), {filename: 'shiftingtiles.less'}, (error, output) => {
      if (error) reject(error);
      else resolve(output.css);
    });
  });
  const style = window.document.createElement('style');
  style.textContent = css;
  window.document.head.appendChild(style);
  const rules = [...style.sheet.cssRules];
  dom.window.close();
  rules.compiledCSS = css;
  return rules;
}

function matchingRules(rules, selector) {
  const matches = [];
  for (const rule of rules) {
    if (rule.selectorText) {
      const normalized = rule.selectorText.replace(/\s+/g, ' ').trim();
      if (normalized === selector || normalized.split(',').map(s => s.trim()).includes(selector)) matches.push(rule);
    }
    if (rule.cssRules) matches.push(...matchingRules([...rule.cssRules], selector));
  }
  return matches;
}

function declaration(rules, selector, property) {
  const values = matchingRules(rules, selector).map(rule => rule.style.getPropertyValue(property).trim()).filter(Boolean);
  assert.ok(values.length, `Missing CSS declaration: ${selector} ${property}`);
  return values.at(-1);
}

test('document declares the mobile viewport', () => {
  const dom = new JSDOM(read('index.html'));
  const meta = dom.window.document.querySelector('meta[name="viewport"]');
  assert.ok(meta, 'viewport meta tag is required');
  assert.match(meta.content, /(?:^|,\s*)width=device-width(?:\s*,|$)/);
  assert.match(meta.content, /(?:^|,\s*)initial-scale=1(?:\s*,|$)/);
  dom.window.close();
});

test('compiled LESS sizes the gallery to the viewport and removes page scroll', async () => {
  const rules = await compiledRules();
  assert.equal(declaration(rules, 'html', 'margin'), '0');
  assert.equal(declaration(rules, 'body', 'margin'), '0');
  assert.equal(declaration(rules, 'html, body', 'overflow'), 'hidden');
  assert.equal(declaration(rules, '.shiftingtiles', 'width'), '100%');
  assert.equal(declaration(rules, '.shiftingtiles', 'height'), '100dvh');
  assert.match(rules.compiledCSS, /height:\s*100vh;\s*height:\s*100dvh;/, '100vh fallback precedes the dynamic viewport override');
});

test('compiled LESS layers each dynamic tile image as blurred cover plus contained full photo', async () => {
  const rules = await compiledRules();
  for (const selector of ['.shiftingtiles > .row .single', '.shiftingtiles > .row .dual > div']) {
    assert.equal(declaration(rules, selector, 'background-size'), '0 0');
    assert.equal(declaration(rules, selector, 'position'), 'relative');
    assert.equal(declaration(rules, selector, 'isolation'), 'isolate');
    assert.equal(declaration(rules, selector, 'overflow'), 'hidden');
  }
  for (const selector of ['.shiftingtiles > .row .single::before', '.shiftingtiles > .row .dual > div::before']) {
    assert.equal(declaration(rules, selector, 'background-image'), 'inherit');
    assert.equal(declaration(rules, selector, 'background-size'), 'cover');
    assert.equal(declaration(rules, selector, 'filter'), 'blur(16px)');
  }
  for (const selector of ['.shiftingtiles > .row .single::after', '.shiftingtiles > .row .dual > div::after']) {
    assert.equal(declaration(rules, selector, 'background-image'), 'inherit');
    assert.equal(declaration(rules, selector, 'background-size'), 'contain');
    assert.equal(declaration(rules, selector, 'background-position'), 'center');
    assert.equal(declaration(rules, selector, 'background-repeat'), 'no-repeat');
  }
});

test('compiled LESS keeps percentage row geometry and existing animation keyframes', async () => {
  const rules = await compiledRules();
  assert.equal(declaration(rules, '.shiftingtiles > .row', 'height'), '50%');
  assert.equal(declaration(rules, '.shiftingtiles > .row', 'width'), '200%');
  const names = rules.filter(rule => rule.type === 7).map(rule => rule.name);
  for (const name of ['leave-l', 'leave-r', 'bounce', 'bounce-small', 'disappear-single', 'disappear-dual']) {
    assert.ok(names.includes(name), `Missing existing keyframes: ${name}`);
  }
});
