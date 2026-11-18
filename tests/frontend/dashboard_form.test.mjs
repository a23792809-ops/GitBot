// Behavioural tests for the task-creation form in the real dashboard script.
//
// The dashboard is a single static HTML file, so these tests extract and run
// the actual <script> against a small DOM/fetch harness. That keeps the tests
// honest without adding a browser or a DOM-library dependency to the project.
//
// Run with: node --test tests/frontend/dashboard_form.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const DASHBOARD = path.join(REPO_ROOT, 'backend', 'gitbot', 'static', 'dashboard', 'index.html');
const TOKEN = 'test-token-that-must-not-leak-into-the-ui';
const API_ORIGIN = 'http://127.0.0.1:8765';

const FORM_IDS = [
    'healthBadge',
    'dryRunBadge',
    'repoCountBadge',
    'aiBadge',
    'schedulerBadge',
    'dryRunBanner',
    'apiToken',
    'connectBtn',
    'disconnectBtn',
    'connectionMsg',
    'systemStatus',
    'refreshSystemBtn',
    'reposList',
    'refreshReposBtn',
    'repoPath',
    'repoSlug',
    'repoBranch',
    'repoAllowCommit',
    'repoTaskTypes',
    'repoTestCommand',
    'addRepoBtn',
    'repoMsg',
    'repoSelect',
    'taskType',
    'taskPrompt',
    'taskSchedule',
    'taskMaxRetries',
    'createTaskBtn',
    'createRunTaskBtn',
    'createTaskMsg',
    'autoRefresh',
    'stateFilter',
    'refreshTasksBtn',
    'schedulerTickBtn',
    'tasksMsg',
    'tasksBody',
    'taskDetailSection',
    'taskDetailSubtitle',
    'closeDetailBtn',
    'taskDetailMsg',
    'taskDetail',
    'refreshApprovalsBtn',
    'approvalsMsg',
    'approvalsBody',
    'loadContributionsBtn',
    'contributionsMsg',
    'contributionsBody',
    'schedulesCard',
    'newScheduleBtn',
    'refreshSchedulesBtn',
    'schedulesMsg',
    'scheduleFormContainer',
    'scheduleFormTitle',
    'scheduleEditId',
    'scheduleRepoSelect',
    'scheduleTaskType',
    'scheduleLocalTime',
    'scheduleTimezone',
    'scheduleDetectedTzHint',
    'schedulePrompt',
    'cancelScheduleBtn',
    'saveScheduleBtn',
    'schedulesBody',
    'scheduleHistorySection',
    'scheduleHistoryTitle',
    'scheduleHistorySubtitle',
    'closeScheduleHistoryBtn',
    'scheduleHistoryMsg',
    'scheduleHistoryBody'
];

const TAG_BY_ID = {
    repoSelect: 'select',
    taskType: 'select',
    stateFilter: 'select',
    tasksBody: 'tbody',
    approvalsBody: 'tbody',
    autoRefresh: 'input',
    repoAllowCommit: 'input',
    repoTestCommand: 'input',
    taskSchedule: 'input',
    taskMaxRetries: 'input',
    scheduleRepoSelect: 'select',
    scheduleTaskType: 'select',
    scheduleLocalTime: 'input',
    scheduleTimezone: 'input',
    schedulePrompt: 'textarea',
    scheduleEditId: 'input',
    schedulesBody: 'tbody',
    scheduleHistoryBody: 'tbody'
};

class ClassList {
    constructor() {
        this.values = new Set();
    }
    add(...names) {
        names.forEach((name) => this.values.add(name));
    }
    remove(...names) {
        names.forEach((name) => this.values.delete(name));
    }
    toggle(name, force) {
        const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
        if (enabled) this.values.add(name);
        else this.values.delete(name);
        return enabled;
    }
    contains(name) {
        return this.values.has(name);
    }
}

class Element {
    constructor(tag, document) {
        this.tagName = String(tag).toUpperCase();
        this.ownerDocument = document;
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.classList = new ClassList();
        this.style = {};
        this.listeners = {};
        this.options = [];
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.textContent = '';
        this.className = '';
        this.title = '';
        this.type = '';
        this.placeholder = '';
        this.htmlFor = '';
        this.colSpan = 1;
        this.hidden = false;
    }

    get id() {
        return this._id || '';
    }
    set id(value) {
        this._id = String(value);
        if (this.ownerDocument && this._id) this.ownerDocument.byId[this._id] = this;
    }

    get childNodes() {
        return this.children;
    }

    appendChild(child) {
        if (!child) return child;
        if (child.tagName === '#FRAGMENT') {
            [...child.children].forEach((nested) => this.appendChild(nested));
            child.children = [];
            return child;
        }
        child.parentNode = this;
        this.children.push(child);
        if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
            this.options.push(child);
        }
        return child;
    }

    prepend(child) {
        if (!child) return child;
        if (child.tagName === '#FRAGMENT') {
            [...child.children].reverse().forEach((nested) => this.prepend(nested));
            child.children = [];
            return child;
        }
        child.parentNode = this;
        this.children.unshift(child);
        if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
            this.options.unshift(child);
        }
        return child;
    }

    replaceChildren(...nodes) {
        this.children = [];
        if (this.tagName === 'SELECT') this.options = [];
        nodes.forEach((child) => this.appendChild(child));
    }

    remove() {
        if (!this.parentNode) return;
        this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
        if (this.parentNode.tagName === 'SELECT') {
            this.parentNode.options = this.parentNode.options.filter((child) => child !== this);
        }
        this.parentNode = null;
    }

    setAttribute(name, value) {
        this.attributes[String(name)] = String(value);
    }

    getAttribute(name) {
        return Object.hasOwn(this.attributes, String(name)) ? this.attributes[String(name)] : null;
    }

    addEventListener(type, handler) {
        const key = String(type);
        this.listeners[key] = this.listeners[key] || [];
        this.listeners[key].push(handler);
    }

    dispatchEvent(event) {
        const payload = {
            type: event.type,
            target: this,
            currentTarget: this,
            preventDefault() {},
            stopPropagation() {}
        };
        (this.listeners[event.type] || []).forEach((handler) => handler(payload));
        return true;
    }

    click() {
        if (this.disabled) return;
        this.dispatchEvent({ type: 'click' });
    }

    querySelector(selector) {
        if (!selector.startsWith('.')) return null;
        const className = selector.slice(1);
        for (const child of this.children) {
            if (String(child.className).split(/\s+/).includes(className)) return child;
            const nested = child.querySelector(selector);
            if (nested) return nested;
        }
        return null;
    }

    scrollIntoView() {}
}

class FakeDocument {
    constructor() {
        this.byId = {};
        this.hidden = false;
        for (const id of FORM_IDS) {
            const element = new Element(TAG_BY_ID[id] || 'div', this);
            element.id = id;
            if (id === 'autoRefresh') element.checked = true;
            if (id === 'taskType') element.value = 'analyze';
            if (id === 'repoBranch') element.value = 'main';
            if (id === 'taskMaxRetries') element.value = '0';
        }
    }

    createElement(tag) {
        return new Element(tag, this);
    }

    createTextNode(text) {
        const node = new Element('#text', this);
        node.textContent = String(text);
        return node;
    }

    createDocumentFragment() {
        return new Element('#fragment', this);
    }

    getElementById(id) {
        return this.byId[String(id)] || null;
    }
}

function jsonResponse(status, body) {
    return {
        ok: status >= 200 && status < 300,
        status,
        headers: {
            get(name) {
                return String(name).toLowerCase() === 'content-type' ? 'application/json' : null;
            }
        },
        async json() {
            return body;
        },
        async text() {
            return JSON.stringify(body);
        }
    };
}

function textOf(element) {
    if (!element) return '';
    return String(element.textContent || '') + element.children.map(textOf).join('');
}

function findButton(root, label) {
    for (const child of root.children || []) {
        if (child.tagName === 'BUTTON' && String(child.textContent).trim() === label) {
            return child;
        }
        const nested = findButton(child, label);
        if (nested) return nested;
    }
    return null;
}

function findInput(root, type) {
    for (const child of root.children || []) {
        if (child.tagName === 'INPUT' && child.type === type) return child;
        const nested = findInput(child, type);
        if (nested) return nested;
    }
    return null;
}

function pathnameOf(url) {
    return new URL(String(url)).pathname;
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

function defaultHandler(state) {
    return (call) => {
        const pathname = pathnameOf(call.url);
        if (call.method === 'GET' && pathname === '/api/health') {
            return jsonResponse(200, {
                status: 'ok',
                version: '0.1.0',
                dry_run: false,
                allowlisted_repos: 1,
                database: { path: '/tmp/gitbot-test.db', migrations: ['0001', '0004'] },
                sandbox: {
                    can_run: true,
                    bwrap_path: '/usr/bin/bwrap',
                    version: 'bubblewrap 0.8.0',
                    user_namespaces: true,
                    filesystem_namespaces: true,
                    network_namespaces: true,
                    detail: ''
                },
                opencode: { available: true, version: '2.0.27', executable: 'opencode' },
                ai: {
                    ready: true,
                    toolchain_ready: true,
                    provider_verified: false,
                    sandbox_network_isolated: true,
                    note: 'toolchain ready; model route not verified'
                },
                scheduler: {
                    enabled: false,
                    interval_seconds: 30,
                    next_scheduled_at: null
                }
            });
        }
        if (call.method === 'GET' && pathname === '/api/repos') {
            return jsonResponse(200, {
                count: 1,
                repos: [
                    {
                        id: 1,
                        slug: 'local/test-repo',
                        path: '/home/om/gitbot-test-repo',
                        default_branch: 'main',
                        allow_commit: false,
                        allow_push: false
                    }
                ]
            });
        }
        if (call.method === 'GET' && pathname === '/api/tasks') {
            return jsonResponse(200, { count: state.createdTasks.length, tasks: [...state.createdTasks] });
        }
        if (call.method === 'GET' && pathname === '/api/tasks/approvals/pending') {
            return jsonResponse(200, { count: 0, approvals: [] });
        }
        if (call.method === 'POST' && pathname === '/api/tasks') {
            const body = JSON.parse(call.body);
            const task = {
                id: 'tsk_dashboard_test',
                repo_id: body.repo_id,
                task_type: body.task_type,
                prompt: body.prompt,
                state: 'queued',
                created_at: '2026-01-01T00:00:00Z',
                updated_at: '2026-01-01T00:00:00Z'
            };
            state.createdTasks.push(task);
            return jsonResponse(200, { task });
        }
        if (call.method === 'POST' && /^\/api\/tasks\/[^/]+\/run$/.test(pathname)) {
            const taskId = pathname.split('/')[3];
            const task = state.createdTasks.find((item) => item.id === taskId);
            if (!task) {
                return jsonResponse(404, { error: { code: 'not_found', message: 'task not found' } });
            }
            task.state = 'awaiting_review';
            return jsonResponse(200, { task });
        }
        if (call.method === 'GET' && /^\/api\/tasks\/[^/]+$/.test(pathname)) {
            const taskId = pathname.split('/')[3];
            const task = state.createdTasks.find((item) => item.id === taskId);
            if (!task) {
                return jsonResponse(404, { error: { code: 'not_found', message: 'task not found' } });
            }
            return jsonResponse(200, { task, events: [] });
        }
        if (call.method === 'GET' && pathname === '/api/auth/check') {
            return jsonResponse(200, { authenticated: true, local_only: true });
        }
        if (call.method === 'GET' && pathname === '/api/schedules') {
            return jsonResponse(200, {
                count: (state.schedules || []).length,
                schedules: [...(state.schedules || [])],
                detected_timezone: 'UTC'
            });
        }
        if (call.method === 'POST' && pathname === '/api/schedules') {
            const body = JSON.parse(call.body);
            const schedule = {
                id: 'sch_dashboard_test',
                repo_id: body.repo_id,
                task_type: body.task_type,
                prompt: body.prompt,
                local_time: body.local_time,
                timezone: body.timezone || 'UTC',
                enabled: body.enabled !== false,
                last_run_at: null,
                last_run_date: null,
                next_run_at: '2026-01-02T09:00:00Z',
                created_at: '2026-01-01T00:00:00Z',
                updated_at: '2026-01-01T00:00:00Z'
            };
            state.schedules = state.schedules || [];
            state.schedules.push(schedule);
            return jsonResponse(201, { schedule });
        }
        if (call.method === 'POST' && /^\/api\/schedules\/[^/]+\/(pause|resume)$/.test(pathname)) {
            const parts = pathname.split('/');
            const scheduleId = parts[3];
            const action = parts[4];
            const schedule = (state.schedules || []).find((s) => s.id === scheduleId);
            if (!schedule) return jsonResponse(404, { error: { code: 'not_found' } });
            schedule.enabled = action === 'resume';
            return jsonResponse(200, { schedule });
        }
        if (call.method === 'DELETE' && /^\/api\/schedules\/[^/]+$/.test(pathname)) {
            const scheduleId = pathname.split('/')[3];
            state.schedules = (state.schedules || []).filter((s) => s.id !== scheduleId);
            return jsonResponse(200, { deleted: true, id: scheduleId });
        }
        if (call.method === 'GET' && /^\/api\/schedules\/[^/]+\/history$/.test(pathname)) {
            const scheduleId = pathname.split('/')[3];
            return jsonResponse(200, { schedule_id: scheduleId, count: 0, history: [] });
        }
        return jsonResponse(404, { error: { code: 'not_found', message: 'not found' } });
    };
}

function createDashboard() {
    const document = new FakeDocument();
    const state = { createdTasks: [], schedules: [] };
    const calls = [];
    let handler = defaultHandler(state);

    const fetchImpl = async (input, init = {}) => {
        const call = {
            url: String(input),
            pathname: pathnameOf(input),
            method: String(init.method || 'GET').toUpperCase(),
            headers: { ...(init.headers || {}) },
            body: init.body
        };
        calls.push(call);
        const result = await handler(call);
        if (result instanceof Error) throw result;
        return result;
    };

    const window = {
        location: { origin: API_ORIGIN },
        addEventListener() {}
    };
    const sandbox = {
        document,
        window,
        fetch: fetchImpl,
        console,
        setTimeout,
        clearTimeout,
        setInterval: () => 1,
        clearInterval: () => {},
        confirm: () => true,
        URL,
        Promise,
        JSON,
        Math,
        Number,
        Date,
        Object,
        Array,
        String,
        Boolean,
        Error,
        TypeError,
        parseInt,
        parseFloat,
        isNaN
    };
    sandbox.globalThis = sandbox;

    const html = fs.readFileSync(DASHBOARD, 'utf8');
    const match = html.match(/<script>([\s\S]*?)<\/script>/);
    assert.ok(match, 'dashboard must contain a script block');
    vm.createContext(sandbox);
    vm.runInContext(match[1], sandbox, { filename: 'dashboard.js' });

    const flush = async (rounds = 20) => {
        for (let i = 0; i < rounds; i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
        }
    };

    return {
        document,
        state,
        calls,
        flush,
        setHandler(next) {
            handler = next;
        },
        async connect(token = TOKEN) {
            document.getElementById('apiToken').value = token;
            document.getElementById('connectBtn').click();
            await flush();
        },
        setForm(prompt, { repoId = '1', taskType = 'docs' } = {}) {
            document.getElementById('repoSelect').value = repoId;
            const typeSelect = document.getElementById('taskType');
            typeSelect.value = taskType;
            typeSelect.dispatchEvent({ type: 'change' });
            document.getElementById('taskPrompt').value = prompt;
        }
    };
}

async function newDashboard() {
    const dashboard = createDashboard();
    await dashboard.flush();
    return dashboard;
}

test('successful creation keeps the description after the API confirms it', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    const createCallsBefore = dashboard.calls.filter(
        (call) => call.method === 'GET' && call.pathname === '/api/tasks'
    ).length;

    dashboard.setForm('Describe the change');
    const prompt = dashboard.document.getElementById('taskPrompt');
    const button = dashboard.document.getElementById('createTaskBtn');
    button.click();

    // The request is in flight: the form is still visible and the button is
    // disabled so a second click cannot create a duplicate task.
    assert.equal(prompt.value, 'Describe the change');
    assert.equal(button.disabled, true);
    assert.match(textOf(dashboard.document.getElementById('createTaskMsg')), /Creating task/);
    button.click();

    await dashboard.flush();

    assert.equal(prompt.value, 'Describe the change');
    assert.match(textOf(dashboard.document.getElementById('createTaskMsg')), /Task created: tsk_dashboard_test/);
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, 'Create task');
    assert.equal(dashboard.document.getElementById('repoSelect').value, '1');
    assert.equal(dashboard.document.getElementById('taskType').value, 'docs');

    const posts = dashboard.calls.filter(
        (call) => call.method === 'POST' && call.pathname === '/api/tasks'
    );
    assert.equal(posts.length, 1, 'duplicate clicks must not create duplicate tasks');
    assert.equal(posts[0].headers.Authorization, 'Bearer ' + TOKEN);
    assert.equal(JSON.parse(posts[0].body).prompt, 'Describe the change');

    const createCallsAfter = dashboard.calls.filter(
        (call) => call.method === 'GET' && call.pathname === '/api/tasks'
    ).length;
    assert.ok(createCallsAfter > createCallsBefore, 'successful creation must refresh the task list');
    assert.equal(dashboard.document.getElementById('tasksBody').children.length, 1);
    assert.match(textOf(dashboard.document.getElementById('tasksBody')), /tsk_dashboar/);
});

test('an authentication failure preserves the description and form values', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && call.pathname === '/api/tasks') {
            return jsonResponse(401, { error: { code: 'unauthorized', message: 'invalid API token' } });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('Keep this on 401', { taskType: 'bugfix' });
    const button = dashboard.document.getElementById('createTaskBtn');
    button.click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Keep this on 401');
    assert.equal(dashboard.document.getElementById('repoSelect').value, '1');
    assert.equal(dashboard.document.getElementById('taskType').value, 'bugfix');
    assert.match(message, /Authentication failed/);
    assert.match(message, /kept/);
    assert.doesNotMatch(message, new RegExp(TOKEN));
    assert.equal(button.disabled, false);
});

test('dry-run disables create & run and preserves the description', async () => {
    const dashboard = createDashboard();
    dashboard.setHandler((call) => {
        if (call.method === 'GET' && call.pathname === '/api/health') {
            return jsonResponse(200, {
                status: 'ok',
                version: '0.1.0',
                dry_run: true,
                allowlisted_repos: 1,
                database: { migrations: ['0001', '0004'] }
            });
        }
        return defaultHandler(dashboard.state)(call);
    });
    await dashboard.flush();
    await dashboard.connect();
    dashboard.setForm('Do not create this in dry-run');
    const runButton = dashboard.document.getElementById('createRunTaskBtn');
    assert.equal(runButton.disabled, true);

    runButton.click();
    await dashboard.flush();

    const posts = dashboard.calls.filter(
        (call) => call.method === 'POST' && call.pathname === '/api/tasks'
    );
    assert.equal(posts.length, 0);
    assert.equal(
        dashboard.document.getElementById('taskPrompt').value,
        'Do not create this in dry-run'
    );
    assert.match(runButton.title, /dry-run is enabled/);
});

test('dry-run allows explain but still blocks write task types', async () => {
    const dashboard = createDashboard();
    dashboard.setHandler((call) => {
        if (call.method === 'GET' && call.pathname === '/api/health') {
            return jsonResponse(200, {
                status: 'ok',
                version: '0.1.0',
                dry_run: true,
                allowlisted_repos: 1,
                database: { migrations: ['0001', '0006'] }
            });
        }
        return defaultHandler(dashboard.state)(call);
    });
    await dashboard.flush();
    await dashboard.connect();

    const runButton = dashboard.document.getElementById('createRunTaskBtn');
    dashboard.setForm('Explain the request flow', { taskType: 'explain' });
    assert.equal(runButton.disabled, false);
    assert.match(runButton.title, /Create this task and run it immediately/);

    runButton.click();
    await dashboard.flush();

    const posts = dashboard.calls.filter(
        (call) => call.method === 'POST' && call.pathname === '/api/tasks'
    );
    assert.equal(posts.length, 1);
    assert.equal(JSON.parse(posts[0].body).task_type, 'explain');

    dashboard.setForm('Change this file', { taskType: 'bugfix' });
    assert.equal(runButton.disabled, true);
    assert.match(runButton.title, /dry-run is enabled/);
});

test('a missing token preserves the description and sends no request', async () => {
    const dashboard = await newDashboard();
    dashboard.setForm('Keep this when disconnected');
    dashboard.document.getElementById('createTaskBtn').click();
    await dashboard.flush();

    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Keep this when disconnected');
    assert.match(textOf(dashboard.document.getElementById('createTaskMsg')), /Not connected/);
    assert.equal(
        dashboard.calls.filter((call) => call.method === 'POST' && call.pathname === '/api/tasks').length,
        0
    );
});

test('a validation failure preserves the description and shows the server message', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && call.pathname === '/api/tasks') {
            return jsonResponse(422, {
                error: { code: 'validation_error', message: 'repo_id is not allowlisted' }
            });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('Keep this on 422');
    dashboard.document.getElementById('createTaskBtn').click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Keep this on 422');
    assert.match(message, /Validation failed/);
    assert.match(message, /repo_id is not allowlisted/);
    assert.match(message, /kept/);
});

test('a network failure preserves the description', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && call.pathname === '/api/tasks') {
            return new TypeError('simulated network down');
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('Keep this on network failure');
    dashboard.document.getElementById('createTaskBtn').click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Keep this on network failure');
    assert.match(message, /Cannot reach the GitBot server/);
    assert.match(message, /kept/);
});

test('a server error preserves the description', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && call.pathname === '/api/tasks') {
            return jsonResponse(500, { error: { code: 'internal_error', message: 'database unavailable' } });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('Keep this on 500');
    dashboard.document.getElementById('createTaskBtn').click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Keep this on 500');
    assert.match(message, /Server error \(HTTP 500\)/);
    assert.match(message, /kept/);
});

test('a refresh failure after creation is not reported as a creation failure', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    let taskListReads = 0;
    dashboard.setHandler((call) => {
        if (call.method === 'GET' && call.pathname === '/api/tasks') {
            taskListReads += 1;
            // The initial refresh already happened; fail the refresh that
            // follows the successful creation.
            if (taskListReads > 1) {
                return jsonResponse(500, { error: { code: 'internal_error', message: 'list unavailable' } });
            }
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('Created even if refresh fails');
    dashboard.document.getElementById('createTaskBtn').click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(
        dashboard.document.getElementById('taskPrompt').value,
        'Created even if refresh fails'
    );
    assert.match(message, /Task created: tsk_dashboard_test/);
    assert.doesNotMatch(message, /Could not create task/);
    assert.equal(dashboard.state.createdTasks.length, 1);
});

test('a slow request keeps the buttons disabled and cannot double-submit', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    const pending = deferred();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && call.pathname === '/api/tasks') {
            return pending.promise;
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('Only create this once');

    const createButton = dashboard.document.getElementById('createTaskBtn');
    const runButton = dashboard.document.getElementById('createRunTaskBtn');
    createButton.click();
    createButton.click();
    runButton.click();

    assert.equal(createButton.disabled, true);
    assert.equal(runButton.disabled, true);
    assert.match(textOf(dashboard.document.getElementById('createTaskMsg')), /Creating task/);

    pending.resolve(
        jsonResponse(200, {
            task: {
                id: 'tsk_slow',
                repo_id: 1,
                task_type: 'docs',
                prompt: 'Only create this once',
                state: 'queued'
            }
        })
    );
    await dashboard.flush();

    const posts = dashboard.calls.filter(
        (call) => call.method === 'POST' && call.pathname === '/api/tasks'
    );
    assert.equal(posts.length, 1);
    assert.equal(createButton.disabled, false);
    assert.equal(runButton.disabled, false);
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Only create this once');
});

test('create & run keeps the description when the run fails', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && /^\/api\/tasks\/[^/]+\/run$/.test(call.pathname)) {
            return jsonResponse(403, {
                error: { code: 'dry_run', message: 'GitBot is in dry-run mode; task execution is disabled.' }
            });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('Create and run this');
    const runButton = dashboard.document.getElementById('createRunTaskBtn');
    runButton.click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Create and run this');
    assert.match(message, /Task created: tsk_dashboard_test/);
    assert.match(message, /run failed/);
    assert.match(message, /description was kept/);
    assert.equal(runButton.disabled, false);

    const createPostsBefore = dashboard.calls.filter(
        (call) => call.method === 'POST' && call.pathname === '/api/tasks'
    ).length;

    // Clicking again with the identical form must not silently create a
    // duplicate task whose first run failed.
    runButton.click();
    await dashboard.flush();
    assert.match(textOf(dashboard.document.getElementById('createTaskMsg')), /already created/);
    assert.equal(
        dashboard.calls.filter((call) => call.method === 'POST' && call.pathname === '/api/tasks').length,
        createPostsBefore
    );
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Create and run this');

    // Changing the description is an explicit choice to create a new task.
    dashboard.setForm('Create and run something else');
    runButton.click();
    await dashboard.flush();
    assert.equal(
        dashboard.calls.filter((call) => call.method === 'POST' && call.pathname === '/api/tasks').length,
        createPostsBefore + 1
    );
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'Create and run something else');
});

test('create & run keeps the description after a successful run', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setForm('Create and run successfully');
    dashboard.document.getElementById('createRunTaskBtn').click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(
        dashboard.document.getElementById('taskPrompt').value,
        'Create and run successfully'
    );
    assert.match(message, /Task created: tsk_dashboard_test/);
    assert.match(message, /state: awaiting_review/);
    assert.match(
        textOf(dashboard.document.getElementById('taskDetailSubtitle')),
        /tsk_dashboard_test/
    );
});

test('system status shows sandbox, AI, migrations, and scheduler without overclaiming', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();

    const status = textOf(dashboard.document.getElementById('systemStatus'));
    assert.match(status, /Sandbox/);
    assert.match(status, /network: yes/);
    assert.match(status, /OpenCode/);
    assert.match(status, /2\.0\.27/);
    assert.match(status, /AI provider route/);
    assert.match(status, /not verified/);
    assert.match(status, /Migrations/);
    assert.match(status, /0001/);
    assert.match(status, /Scheduler/);
    assert.match(status, /off/);
    assert.doesNotMatch(status, /model route verified/i);
});

test('create & run preserves the description when the task ends in a failed state', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && /^\/api\/tasks\/[^/]+\/run$/.test(call.pathname)) {
            const task = dashboard.state.createdTasks[0];
            task.state = 'failed';
            return jsonResponse(200, { task });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.setForm('This run will fail');
    dashboard.document.getElementById('createRunTaskBtn').click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('createTaskMsg'));
    assert.equal(dashboard.document.getElementById('taskPrompt').value, 'This run will fail');
    assert.match(message, /state: failed/);
    assert.match(message, /description was kept/);

    const createPostsBefore = dashboard.calls.filter(
        (call) => call.method === 'POST' && call.pathname === '/api/tasks'
    ).length;
    dashboard.document.getElementById('createRunTaskBtn').click();
    await dashboard.flush();
    assert.match(textOf(dashboard.document.getElementById('createTaskMsg')), /already created/);
    assert.equal(
        dashboard.calls.filter((call) => call.method === 'POST' && call.pathname === '/api/tasks').length,
        createPostsBefore
    );
});

test('repo add button sends the selected policy and refreshes the list', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && call.pathname === '/api/repos') {
            return jsonResponse(200, { repo: { id: 2, slug: 'local/new-repo' } });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.document.getElementById('repoPath').value = '/home/om/new-repo';
    dashboard.document.getElementById('repoSlug').value = 'local/new-repo';
    dashboard.document.getElementById('repoTestCommand').value = 'pytest -q';
    dashboard.document.getElementById('addRepoBtn').click();
    await dashboard.flush();

    const posts = dashboard.calls.filter(
        (call) => call.method === 'POST' && call.pathname === '/api/repos'
    );
    assert.equal(posts.length, 1);
    const body = JSON.parse(posts[0].body);
    assert.equal(body.path, '/home/om/new-repo');
    assert.equal(body.slug, 'local/new-repo');
    assert.equal(body.test_command, 'pytest -q');
    assert.deepEqual(body.allowed_task_types.sort(), ['analyze', 'bugfix', 'deps', 'docs', 'explain', 'feature', 'tests']);
    assert.match(textOf(dashboard.document.getElementById('repoMsg')), /allowlisted/);
    assert.equal(dashboard.document.getElementById('repoPath').value, '');
});

test('task list run and cancel buttons call the correct endpoints', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    const task = {
        id: 'tsk_actions',
        repo_id: 1,
        task_type: 'docs',
        prompt: 'action task',
        state: 'queued',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z'
    };
    dashboard.state.createdTasks.push(task);
    dashboard.setHandler((call) => {
        if (call.method === 'POST' && call.pathname === '/api/tasks/tsk_actions/run') {
            task.state = 'awaiting_review';
            return jsonResponse(200, { task });
        }
        if (call.method === 'POST' && call.pathname === '/api/tasks/tsk_actions/cancel') {
            task.state = 'cancelled';
            return jsonResponse(200, { task });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.document.getElementById('refreshTasksBtn').click();
    await dashboard.flush();

    const body = dashboard.document.getElementById('tasksBody');
    const runButton = findButton(body, 'Run');
    assert.ok(runButton, 'queued task must expose a Run button');
    runButton.click();
    await dashboard.flush();
    assert.ok(
        dashboard.calls.some(
            (call) => call.method === 'POST' && call.pathname === '/api/tasks/tsk_actions/run'
        )
    );
    assert.equal(task.state, 'awaiting_review');
    assert.match(textOf(dashboard.document.getElementById('tasksBody')), /Awaiting review/i);

    dashboard.document.getElementById('refreshTasksBtn').click();
    await dashboard.flush();
    const cancelButton = findButton(dashboard.document.getElementById('tasksBody'), 'Cancel');
    assert.ok(cancelButton, 'review task must expose a Cancel button');
    cancelButton.click();
    await dashboard.flush();
    assert.ok(
        dashboard.calls.some(
            (call) => call.method === 'POST' && call.pathname === '/api/tasks/tsk_actions/cancel'
        )
    );
});

test('approval and commit buttons send the exact diff hash and task id', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();
    const task = {
        id: 'tsk_review',
        repo_id: 1,
        task_type: 'feature',
        prompt: 'review task',
        state: 'awaiting_review',
        approval_id: 'apr_review',
        diff_hash: 'a'.repeat(64),
        diff_patch: 'diff --git a/file b/file\n+new\n',
        diff_files: '["file"]',
        diff_truncated: 0,
        sandboxed: 1,
        snapshot_head: 'b'.repeat(40),
        tests_status: 'passed',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z'
    };
    dashboard.state.createdTasks.push(task);
    dashboard.setHandler((call) => {
        if (call.method === 'GET' && call.pathname === '/api/repos') {
            return jsonResponse(200, {
                count: 1,
                repos: [{
                    id: 1,
                    slug: 'local/test-repo',
                    path: '/home/om/gitbot-test-repo',
                    default_branch: 'main',
                    allow_commit: true,
                    allow_push: false
                }]
            });
        }
        if (call.method === 'POST' && call.pathname === '/api/tasks/approvals/apr_review/approve') {
            task.state = 'approved';
            return jsonResponse(200, { approved: true, approval_id: 'apr_review' });
        }
        if (call.method === 'POST' && call.pathname === '/api/tasks/tsk_review/commit') {
            task.state = 'committed';
            return jsonResponse(200, { task });
        }
        if (call.method === 'GET' && call.pathname === '/api/tasks/tsk_review') {
            return jsonResponse(200, {
                task,
                events: [],
                diff: {
                    patch: task.diff_patch,
                    files: ['file'],
                    truncated: false,
                    staged: false,
                    stored: true
                }
            });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.document.getElementById('refreshReposBtn').click();
    dashboard.document.getElementById('refreshTasksBtn').click();
    await dashboard.flush();
    const reviewButton = findButton(dashboard.document.getElementById('tasksBody'), 'Review');
    assert.ok(reviewButton, 'review task must expose a Review button');
    reviewButton.click();
    await dashboard.flush();

    const detail = dashboard.document.getElementById('taskDetail');
    const confirmBox = findInput(detail, 'checkbox');
    assert.ok(confirmBox, 'approval requires an explicit confirmation checkbox');
    confirmBox.checked = true;
    confirmBox.dispatchEvent({ type: 'change' });
    const approveButton = findButton(detail, 'Approve diff');
    assert.ok(approveButton, 'approval button must be rendered');
    assert.equal(approveButton.disabled, false);
    approveButton.click();
    await dashboard.flush();

    const approveCall = dashboard.calls.find(
        (call) => call.method === 'POST' && call.pathname === '/api/tasks/approvals/apr_review/approve'
    );
    assert.ok(approveCall, 'approve button must call the approval endpoint');
    assert.deepEqual(JSON.parse(approveCall.body), { diff_hash: task.diff_hash });

    const commitButton = findButton(dashboard.document.getElementById('taskDetail'), 'Approve & commit locally');
    assert.ok(commitButton, 'approved task must expose the local commit button');
    commitButton.click();
    await dashboard.flush();
    assert.ok(
        dashboard.calls.some(
            (call) => call.method === 'POST' && call.pathname === '/api/tasks/tsk_review/commit'
        )
    );
    assert.match(textOf(dashboard.document.getElementById('taskDetailMsg')), /Committed locally/);
});

test('sensitive reads send the bearer token once connected', async () => {
    const dashboard = await newDashboard();
    await dashboard.connect();

    const sensitive = dashboard.calls.filter(
        (call) =>
            call.method === 'GET' &&
            ['/api/repos', '/api/tasks', '/api/tasks/approvals/pending'].includes(call.pathname)
    );
    assert.ok(sensitive.length >= 3, 'connected refresh must read repos, tasks, and approvals');
    sensitive.forEach((call) => {
        assert.equal(call.headers.Authorization, 'Bearer ' + TOKEN);
    });
});

test('sensitive reads are not sent and the UI asks for a token when disconnected', async () => {
    const dashboard = createDashboard();
    await dashboard.flush();

    const dataReads = dashboard.calls.filter(
        (call) =>
            call.method === 'GET' &&
            ['/api/repos', '/api/tasks', '/api/tasks/approvals/pending'].includes(call.pathname)
    );
    assert.equal(dataReads.length, 0, 'no token means no task/repo/approval request');
    assert.ok(
        dashboard.calls.some((call) => call.method === 'GET' && call.pathname === '/api/health'),
        'health stays public'
    );
    assert.match(
        textOf(dashboard.document.getElementById('tasksMsg')),
        /Connect with your API token/
    );
    assert.match(
        textOf(dashboard.document.getElementById('reposList')),
        /Connect with your API token/
    );
});

test('a rejected read token keeps the connect guidance instead of crashing', async () => {
    const dashboard = await newDashboard();
    dashboard.setHandler((call) => {
        if (call.method === 'GET' && call.pathname === '/api/tasks') {
            return jsonResponse(401, {
                error: { code: 'unauthorized', message: 'invalid API token' }
            });
        }
        return defaultHandler(dashboard.state)(call);
    });
    dashboard.document.getElementById('apiToken').value = TOKEN;
    dashboard.document.getElementById('connectBtn').click();
    await dashboard.flush();

    const message = textOf(dashboard.document.getElementById('tasksMsg'));
    assert.match(message, /Connect with your API token/);
    assert.doesNotMatch(message, new RegExp(TOKEN));
});

test('daily automation schedules list and form creation', async () => {
    const dashboard = await newDashboard();
    dashboard.document.getElementById('apiToken').value = TOKEN;
    dashboard.document.getElementById('connectBtn').click();
    await dashboard.flush();

    // Verify GET /api/schedules was called
    const scheduleCalls = dashboard.calls.filter(
        (c) => c.method === 'GET' && c.pathname === '/api/schedules'
    );
    assert.ok(scheduleCalls.length > 0, 'loadSchedules requested /api/schedules');

    // Click new schedule button
    dashboard.document.getElementById('newScheduleBtn').click();
    const formContainer = dashboard.document.getElementById('scheduleFormContainer');
    assert.ok(!formContainer.classList.contains('hidden'), 'form is visible');

    // Fill form
    dashboard.document.getElementById('scheduleRepoSelect').value = '1';
    dashboard.document.getElementById('scheduleTaskType').value = 'analyze';
    dashboard.document.getElementById('scheduleLocalTime').value = '09:00';
    dashboard.document.getElementById('schedulePrompt').value = 'Run daily automated tests';

    // Click save
    dashboard.document.getElementById('saveScheduleBtn').click();
    await dashboard.flush();

    const postCall = dashboard.calls.find(
        (c) => c.method === 'POST' && c.pathname === '/api/schedules'
    );
    assert.ok(postCall, 'POST /api/schedules was called');
    const body = JSON.parse(postCall.body);
    assert.equal(body.repo_id, 1);
    assert.equal(body.task_type, 'analyze');
    assert.equal(body.local_time, '09:00');
    assert.equal(body.prompt, 'Run daily automated tests');
});

test('daily automation pause, resume, delete and history actions', async () => {
    const dashboard = await newDashboard();
    // Pre-populate a schedule in state
    dashboard.state.schedules = [
        {
            id: 'sch_action_test',
            repo_id: 1,
            task_type: 'analyze',
            prompt: 'Test prompt',
            local_time: '10:00',
            timezone: 'UTC',
            enabled: true,
            last_run_at: null,
            last_run_date: null,
            next_run_at: '2026-01-02T10:00:00Z',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z'
        }
    ];

    dashboard.document.getElementById('apiToken').value = TOKEN;
    dashboard.document.getElementById('connectBtn').click();
    await dashboard.flush();

    const tbody = dashboard.document.getElementById('schedulesBody');
    assert.ok(tbody.children.length > 0, 'schedules row rendered');
    const row = tbody.children[0];

    // Find Pause button (first button in actions row)
    const buttons = [];
    function findButtons(el) {
        for (const child of el.children || []) {
            if (child.tagName === 'BUTTON') buttons.push(child);
            findButtons(child);
        }
    }
    findButtons(row);

    // [Pause, Edit, Delete, History]
    const pauseBtn = buttons.find((b) => textOf(b) === 'Pause');
    assert.ok(pauseBtn, 'Pause button found');
    pauseBtn.click();
    await dashboard.flush();

    const pauseCall = dashboard.calls.find(
        (c) => c.method === 'POST' && c.pathname === '/api/schedules/sch_action_test/pause'
    );
    assert.ok(pauseCall, 'POST /api/schedules/sch_action_test/pause was called');

    // History button
    const historyBtn = buttons.find((b) => textOf(b) === 'History');
    assert.ok(historyBtn, 'History button found');
    historyBtn.click();
    await dashboard.flush();

    const histCall = dashboard.calls.find(
        (c) => c.method === 'GET' && c.pathname === '/api/schedules/sch_action_test/history'
    );
    assert.ok(histCall, 'GET /api/schedules/sch_action_test/history was called');

    // Delete button
    const deleteBtn = buttons.find((b) => textOf(b) === 'Delete');
    assert.ok(deleteBtn, 'Delete button found');
    deleteBtn.click();
    await dashboard.flush();

    const deleteCall = dashboard.calls.find(
        (c) => c.method === 'DELETE' && c.pathname === '/api/schedules/sch_action_test'
    );
    assert.ok(deleteCall, 'DELETE /api/schedules/sch_action_test was called');
});
