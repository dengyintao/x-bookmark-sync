const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/xbm/web/app.js'), 'utf8');
const routes = source.slice(source.indexOf('const servicePrefix ='), source.indexOf('async function refreshAccounts()'));
for (const pathname of ['/', '/app/x-bookmark-sync', '/app/x-bookmark-sync/']) {
 test(`dashboard API and media links retain service mount at ${pathname}`, () => {
  const ctx = vm.createContext({URL, location:{pathname,origin:'https://nas.example'}, selectedAccount:'x:fixture'});
  vm.runInContext(routes,ctx);
  const prefix = pathname === '/' ? '' : '/app/x-bookmark-sync';
  assert.equal(vm.runInContext("serviceUrl('/api/access/status')",ctx),prefix+'/api/access/status');
  assert.equal(vm.runInContext("accountUrl('/api/media/42')",ctx),prefix+'/api/media/42?account=x%3Afixture');
  assert.equal(vm.runInContext("accountUrl('/api/runs?limit=1')",ctx),prefix+'/api/runs?limit=1&account=x%3Afixture');
 });
}
