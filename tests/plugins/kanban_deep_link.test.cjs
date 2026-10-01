// Execute the shipped plugin through its SDK registration, not source matching.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

async function mount(search) {
  let Page, cursor = 0;
  const state = [], effects = [];
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in state)) state[i] = typeof initial === 'function' ? initial() : initial;
      return [state[i], value => {state[i] = typeof value === 'function' ? value(state[i]) : value;}];
    },
    useRef(value) { return hooks.useState(() => ({current:value}))[0]; },
    useCallback(fn) { return fn; },
    useMemo(fn) { return fn(); },
    useEffect(fn) { effects.push(fn); },
  };
  const window = {
    location: {search, protocol: 'http:', host: 'localhost'},
    localStorage: {getItem() { return null; }},
    __HERMES_PLUGINS__: {register(id, page) { assert.equal(id, 'kanban'); Page = page; }},
    __HERMES_PLUGIN_SDK__: {
      React: {...hooks, Component: class {}, createElement: (type, props, ...children) => ({type, props, children})},
      hooks, components: {}, utils: {cn: (...args) => args.join(' ')},
      fetchJSON: async url => url.includes('/boards') ? {boards: [], current:'default'} : url.includes('/board') ? {columns: [], assignees: [], tenants: []} : {},
    },
  };
  const context = vm.createContext({window, URL, URLSearchParams, console, setTimeout, clearTimeout});
  vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../plugins/kanban/dashboard/dist/index.js'), 'utf8'), context);
  Page();
  for (const effect of effects.splice(0)) effect();
  await new Promise(resolve => setImmediate(resolve));
  const render = () => { cursor = 0; return Page(); };
  return {render};
}
function drawer(node) {
  if (!node || typeof node !== 'object') return null;
  if (node.type?.name === 'TaskDrawer') return node;
  for (const child of node.children || []) { const found = drawer(child); if (found) return found; }
  return null;
}
for (const query of ['?task=t_4306f854', '?other=x&task=t_4306f854', '?task=t_00000000']) {
  test('opens drawer independently of visible cards: ' + query, async () => {
    const {render} = await mount(query);
    const opened = drawer(render());
    assert.equal(opened?.props.taskId, new URLSearchParams(query).get('task'));
    opened.props.onClose();
    assert.equal(drawer(render()), null);
  });
}
for (const query of ['', '?task=', '?task=t_BAD', '?task=../../bad', '?task=t_4306f854extra', '?task=%3Cscript%3E']) {
  test('does not open malformed or absent task: ' + query, async () => {
    const {render} = await mount(query);
    assert.equal(drawer(render()), null);
  });
}
