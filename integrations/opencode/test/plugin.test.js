// Tests of the OpenCode 2.x plugin against a fake 2.x context and a fake greenproof
// executable. Nothing here runs OpenCode: see scripts/e2e/opencode.py for that.
import assert from "node:assert/strict"
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import path from "node:path"
import { after, afterEach, beforeEach, describe, mock, test } from "node:test"
import { fileURLToPath, pathToFileURL } from "node:url"

const SOURCE = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "index.js")
const ROOT = mkdtempSync(path.join(tmpdir(), "greenproof-opencode-"))
const REGISTRY = Symbol.for("greenproof.opencode")
let counter = 0

after(() => rmSync(ROOT, { recursive: true, force: true }))

function freshDir(label) {
  const dir = path.join(ROOT, `${label}-${++counter}`)
  mkdirSync(dir, { recursive: true })
  return dir
}

// A fake greenproof: records every call (argv, stdin, cwd) to calls.jsonl and answers
// according to config.json, both next to the script.
function fakeGreenproof(behavior = {}) {
  const dir = freshDir("fake")
  const script = path.join(dir, "greenproof")
  const code = `#!${process.execPath}
const fs = require("node:fs")
const path = require("node:path")
const dir = ${JSON.stringify(dir)}
const config = JSON.parse(fs.readFileSync(path.join(dir, "config.json"), "utf8"))
const args = process.argv.slice(2)
const mode = (config[args.includes("check") ? "check" : "start"]) || "pass"
let stdin = ""
const respond = () => {
  fs.appendFileSync(path.join(dir, "calls.jsonl"), JSON.stringify({ args, stdin, cwd: process.cwd() }) + "\\n")
  if (mode === "pass") return
  if (mode === "block") return process.stdout.write(JSON.stringify({ decision: "block", reason: "greenproof: not done.\\n  BLOCK tests.skipped" }))
  if (mode === "release") return process.stdout.write(JSON.stringify({ continue: false, stopReason: "greenproof: the agent stopped with the gate failing" }))
  if (mode === "garbage") return process.stdout.write("not json")
  if (mode === "scalar") return process.stdout.write("7")
  if (mode === "unknown") return process.stdout.write(JSON.stringify({ hello: "world" }))
  if (mode === "error") { process.stderr.write("boom happened"); process.exit(3) }
  if (mode === "hang") return setInterval(() => {}, 1000)
  if (mode === "slow") return setTimeout(() => {}, ${behavior.delayMs ?? 300})
}
if (mode === "quit") { fs.appendFileSync(path.join(dir, "calls.jsonl"), JSON.stringify({ args, stdin: "", cwd: process.cwd() }) + "\\n"); process.exit(0) }
process.stdin.setEncoding("utf8")
process.stdin.on("data", (chunk) => { stdin += chunk })
process.stdin.on("end", () => {
  if (mode === "slow") { respond(); process.stdout.write(JSON.stringify({ decision: "block", reason: "slow block" })) } else respond()
})
`
  writeFileSync(script, code)
  chmodSync(script, 0o755)
  writeFileSync(path.join(dir, "config.json"), JSON.stringify(behavior))
  return {
    dir,
    script,
    calls: () => {
      try {
        return readFileSync(path.join(dir, "calls.jsonl"), "utf8")
          .split("\n")
          .filter(Boolean)
          .map((line) => {
            const call = JSON.parse(line)
            return { ...call, stdin: call.stdin ? JSON.parse(call.stdin) : null }
          })
      } catch {
        return []
      }
    },
  }
}

// The plugin source with COMMAND replaced, written next to a package.json so Node loads it
// as an ES module. Each call is a separate module instance, like a second copy would be.
async function loadPlugin(command) {
  const source = readFileSync(SOURCE, "utf8")
  const lines = source.split("\n").filter((line) => line.startsWith("const COMMAND = "))
  assert.equal(lines.length, 1)
  const dir = freshDir("plugin")
  writeFileSync(path.join(dir, "package.json"), '{"type": "module"}')
  const file = path.join(dir, "greenproof.js")
  writeFileSync(file, source.replace(lines[0], `const COMMAND = ${JSON.stringify(command)}`))
  return import(pathToFileURL(file).href)
}

// An async iterable of events that the test pushes into; closes on abort or return().
function eventStream(signal) {
  const queue = []
  const waiting = []
  let done = false
  const close = () => {
    done = true
    for (const resolve of waiting.splice(0)) resolve({ done: true, value: undefined })
  }
  signal?.addEventListener("abort", close, { once: true })
  return {
    push(event) {
      const resolve = waiting.shift()
      if (resolve) resolve({ done: false, value: event })
      else queue.push(event)
    },
    [Symbol.asyncIterator]() {
      return {
        next: () => {
          if (queue.length) return Promise.resolve({ done: false, value: queue.shift() })
          if (done) return Promise.resolve({ done: true, value: undefined })
          return new Promise((resolve) => waiting.push(resolve))
        },
        return: () => {
          close()
          return Promise.resolve({ done: true, value: undefined })
        },
      }
    },
  }
}

// A fake 2.x plugin context. `sessions` maps id to Session.Info, `messages` maps id to the
// SessionMessage.Info list of ctx.session.context.
function fakeContext(options = {}) {
  const directory = options.directory ?? freshDir("project")
  const state = {
    directory,
    hooks: {},
    synthetic: [],
    disposed: 0,
    subscriptions: [],
    sessions: options.sessions ?? {},
    messages: options.messages ?? {},
  }
  const session = {
    get: async ({ sessionID }) => {
      if (options.getThrows) throw new Error("session lookup failed")
      return state.sessions[sessionID] ?? { id: sessionID }
    },
    context: async ({ sessionID }) => {
      if (options.contextThrows) throw new Error("context failed")
      return state.messages[sessionID] ?? []
    },
    synthetic: async (input) => {
      if (options.syntheticThrows) throw new Error("inbox closed")
      state.synthetic.push(input)
    },
    hook: async (name, callback) => {
      if (options.hookThrows) throw new Error("registration refused")
      state.hooks[name] = callback
      return { dispose: async () => void state.disposed++ }
    },
  }
  if (options.withoutHook) delete session.hook
  const ctx = {
    location: { directory },
    options: {},
    session,
    event: {
      subscribe: (input) => {
        const stream = eventStream(input?.signal)
        state.subscriptions.push({ input, stream })
        return stream
      },
    },
  }
  if (options.withoutEvents) delete ctx.event
  return { ctx, state }
}

const assistant = (id) => ({ type: "assistant", id, content: [] })
const user = (id) => ({ type: "user", id })
const created = (sessionID, extra = {}) => ({
  type: "session.created",
  data: { sessionID, location: { directory: "/x" }, ...extra },
})

// Lets pending promises, microtasks and I/O callbacks run; polls until `until` holds.
async function settle(until = () => false, limit = 5000) {
  const deadline = Date.now() + limit
  do {
    await new Promise((resolve) => setImmediate(resolve))
    if (until()) return true
  } while (Date.now() < deadline)
  return until()
}

let warnings
beforeEach(() => {
  delete globalThis[REGISTRY]
  warnings = []
  mock.method(console, "warn", (...args) => void warnings.push(args.join(" ")))
})
afterEach(() => {
  mock.restoreAll()
  mock.timers.reset()
})

async function start(behavior, ctxOptions, command) {
  const fake = fakeGreenproof(behavior)
  const plugin = await loadPlugin(command ?? [fake.script])
  const made = fakeContext(ctxOptions)
  const cleanup = await plugin.default.setup(made.ctx)
  return { fake, plugin, cleanup, ...made }
}

describe("module shape", () => {
  test("the only export is default { id, setup }", async () => {
    const plugin = await loadPlugin(["greenproof"])
    assert.deepEqual(Object.keys(plugin), ["default"])
    assert.deepEqual(Object.keys(plugin.default).sort(), ["id", "setup"])
    assert.equal(plugin.default.id, "greenproof")
    assert.equal(typeof plugin.default.setup, "function")
  })

  test("the source imports only node: built-ins and has one COMMAND line", () => {
    const source = readFileSync(SOURCE, "utf8")
    const specifiers = [...source.matchAll(/^import .* from "([^"]+)"/gm)].map((m) => m[1])
    assert.ok(specifiers.length > 0)
    for (const specifier of specifiers) assert.match(specifier, /^node:/)
    assert.equal(source.split("\n").filter((line) => line.startsWith("const COMMAND = ")).length, 1)
    assert.equal((source.match(/^export /gm) ?? []).length, 1)
  })

  test("setup registers the experimental idle hook and returns a cleanup", async () => {
    const { state, cleanup } = await start()
    assert.deepEqual(Object.keys(state.hooks), ["experimental.idle"])
    assert.equal(typeof cleanup, "function")
    assert.deepEqual(state.subscriptions[0].input.types, ["session.created"])
  })

  test("cleanup stops the event subscription and disposes the hook", async () => {
    const { state, cleanup, fake } = await start()
    await cleanup()
    assert.equal(state.disposed, 1)
    assert.equal(state.subscriptions[0].input.signal.aborted, true)
    state.subscriptions[0].stream.push(created("ses_late"))
    await settle(() => false, 50)
    assert.equal(fake.calls().length, 0)
  })
})

describe("idle: the gate", () => {
  test("pass: nothing is added to the session, and the payload is the documented one", async () => {
    const { state, fake } = await start({ check: "pass" }, { messages: { ses_1: [user("m0"), assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    const calls = fake.calls()
    assert.equal(calls.length, 1)
    assert.deepEqual(calls[0].args, ["check", "--hook", "opencode"])
    assert.deepEqual(calls[0].stdin, {
      session_id: "ses_1",
      cwd: state.directory,
      hook_event_name: "session.idle",
      message_id: "msg_a",
    })
    assert.equal(await realpath(calls[0].cwd), await realpath(state.directory))
    assert.equal(state.synthetic.length, 0)
    assert.deepEqual(warnings, [])
  })

  test("block: the reason continues the same run through ctx.session.synthetic", async () => {
    const { state, fake } = await start({ check: "block" }, { messages: { ses_1: [assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.equal(fake.calls().length, 1)
    assert.deepEqual(state.synthetic, [
      { sessionID: "ses_1", text: "greenproof: not done.\n  BLOCK tests.skipped" },
    ])
    assert.deepEqual(warnings, [])
  })

  test("release: the note is a console warning and the session is left alone", async () => {
    const { state } = await start({ check: "release" }, { messages: { ses_1: [assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.equal(state.synthetic.length, 0)
    assert.deepEqual(warnings, ["greenproof: the agent stopped with the gate failing"])
  })

  test("the last assistant message is used, and a later stop with a new message runs again", async () => {
    const { state, fake } = await start({ check: "pass" }, { messages: { ses_1: [assistant("msg_a"), user("u"), assistant("msg_b")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    state.messages.ses_1.push(user("u2"), assistant("msg_c"))
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.deepEqual(
      fake.calls().map((call) => call.stdin.message_id),
      ["msg_b", "msg_c"],
    )
  })

  test("a COMMAND prefix is passed before the subcommand, with no shell", async () => {
    const fake = fakeGreenproof({ check: "pass" })
    const { ctx, state } = fakeContext({ messages: { ses_1: [assistant("msg_a")] } })
    const plugin = await loadPlugin([fake.script, "x; echo pwned", "$(touch /nonexistent/never)"])
    await plugin.default.setup(ctx)
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.deepEqual(fake.calls()[0].args, [
      "x; echo pwned",
      "$(touch /nonexistent/never)",
      "check",
      "--hook",
      "opencode",
    ])
  })

  test("an event without a session id does nothing", async () => {
    const { state, fake } = await start({ check: "pass" })
    await state.hooks["experimental.idle"]({ background: [] })
    await state.hooks["experimental.idle"](undefined)
    assert.equal(fake.calls().length, 0)
  })
})

describe("idle: failing open", () => {
  for (const [mode, expected] of [
    ["error", /greenproof could not run: exit 3: boom happened/],
    ["garbage", /greenproof could not run: its output was not valid JSON/],
    ["scalar", /greenproof could not run: its output was not an object/],
    ["unknown", /greenproof could not run: its output was not a known answer/],
  ]) {
    test(`${mode}: a warning, nothing else`, async () => {
      const { state } = await start({ check: mode }, { messages: { ses_1: [assistant("msg_a")] } })
      await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
      assert.equal(state.synthetic.length, 0)
      assert.equal(warnings.length, 1)
      assert.match(warnings[0], expected)
    })
  }

  test("a command that cannot be spawned", async () => {
    const { state } = await start({}, { messages: { ses_1: [assistant("msg_a")] } }, [
      path.join(ROOT, "no-such-greenproof"),
    ])
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.equal(state.synthetic.length, 0)
    assert.match(warnings[0], /greenproof could not run: .*ENOENT/)
  })

  test("a project directory that does not exist", async () => {
    const { state } = await start({ check: "pass" }, {
      directory: path.join(ROOT, "missing-directory"),
      messages: { ses_1: [assistant("msg_a")] },
    })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.match(warnings[0], /greenproof could not run/)
  })

  test("a child that exits without reading stdin", async () => {
    const { state, fake } = await start({ check: "quit" }, { messages: { ses_1: [assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.equal(fake.calls().length, 1)
    assert.deepEqual(warnings, [])
  })

  test("a check that runs past 3600 seconds is killed and fails open", async () => {
    const { state, fake } = await start({ check: "hang" }, { messages: { ses_1: [assistant("msg_a")] } })
    mock.timers.enable({ apis: ["setTimeout"] })
    const idle = state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.ok(await settle(() => fake.calls().length === 1), "the fake was never started")
    mock.timers.tick(3600 * 1000 - 1)
    await settle(() => false, 20)
    assert.equal(warnings.length, 0)
    mock.timers.tick(1)
    await idle
    assert.equal(state.synthetic.length, 0)
    assert.deepEqual(warnings, ["greenproof could not run: timed out after 3600 seconds"])
    mock.timers.tick(10_000)
  })

  test("a failing ctx.session.synthetic does not reject the hook", async () => {
    const { state } = await start({ check: "block" }, { syntheticThrows: true, messages: { ses_1: [assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.match(warnings[0], /greenproof could not run: inbox closed/)
  })
})

describe("idle: which sessions and events", () => {
  test("a child session (subagent) is skipped", async () => {
    const { state, fake } = await start({ check: "block" }, {
      sessions: { ses_child: { id: "ses_child", parentID: "ses_1" } },
      messages: { ses_child: [assistant("msg_a")] },
    })
    await state.hooks["experimental.idle"]({ sessionID: "ses_child", background: [] })
    assert.equal(fake.calls().length, 0)
    assert.equal(state.synthetic.length, 0)
  })

  test("a session that cannot be read is skipped with a warning", async () => {
    const { state, fake } = await start({ check: "block" }, { getThrows: true })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.equal(fake.calls().length, 0)
    assert.match(warnings[0], /could not read the session/)
  })

  test("a missing message id runs nothing and says why", async () => {
    for (const messages of [[], [user("u1")], [{ type: "assistant" }], "nonsense"]) {
      delete globalThis[REGISTRY]
      warnings.length = 0
      const { state, fake } = await start({ check: "block" }, { messages: { ses_1: messages } })
      await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
      assert.equal(fake.calls().length, 0)
      assert.equal(state.synthetic.length, 0)
      assert.match(warnings[0], /id of the last assistant message could not be read/)
    }
  })

  test("messages that cannot be read run nothing", async () => {
    const { state, fake } = await start({ check: "block" }, { contextThrows: true })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1", background: [] })
    assert.equal(fake.calls().length, 0)
    assert.match(warnings[0], /could not read the session messages: context failed/)
    assert.match(warnings[1], /did not run/)
  })

  test("a concurrent idle event for a session whose check is running is ignored", async () => {
    const { state, fake } = await start({ check: "slow", delayMs: 300 }, { messages: { ses_1: [assistant("msg_a")] } })
    const hook = state.hooks["experimental.idle"]
    const first = hook({ sessionID: "ses_1", background: [] })
    const second = hook({ sessionID: "ses_1", background: [] })
    const third = hook({ sessionID: "ses_1", background: [] })
    await Promise.all([first, second, third])
    assert.equal(fake.calls().length, 1)
    assert.equal(state.synthetic.length, 1)
    // Afterwards a new stop is checked again.
    state.messages.ses_1.push(assistant("msg_b"))
    await hook({ sessionID: "ses_1", background: [] })
    assert.equal(fake.calls().length, 2)
  })

  test("two sessions are checked independently and at the same time", async () => {
    const { state, fake } = await start({ check: "slow", delayMs: 300 }, {
      messages: { ses_1: [assistant("msg_a")], ses_2: [assistant("msg_b")] },
    })
    const hook = state.hooks["experimental.idle"]
    await Promise.all([hook({ sessionID: "ses_1" }), hook({ sessionID: "ses_2" })])
    assert.deepEqual(fake.calls().map((call) => call.stdin.session_id).sort(), ["ses_1", "ses_2"])
  })

  test("the same stop seen again (same message id) is not checked twice", async () => {
    const { state, fake } = await start({ check: "pass" }, { messages: { ses_1: [assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    assert.equal(fake.calls().length, 1)
  })
})

describe("two copies in one process", () => {
  async function twoCopies(behavior, ctxOptions) {
    const fake = fakeGreenproof(behavior)
    const [first, second] = await Promise.all([loadPlugin([fake.script]), loadPlugin([fake.script])])
    assert.notEqual(first, second)
    const one = fakeContext(ctxOptions)
    const two = fakeContext({ ...ctxOptions, directory: one.state.directory })
    await first.default.setup(one.ctx)
    await second.default.setup(two.ctx)
    return { fake, one, two }
  }

  test("one stop gives one check and one continuation", async () => {
    const messages = { ses_1: [assistant("msg_a")] }
    const { fake, one, two } = await twoCopies({ check: "slow", delayMs: 200 }, { messages })
    two.state.messages = one.state.messages
    await Promise.all([
      one.state.hooks["experimental.idle"]({ sessionID: "ses_1" }),
      two.state.hooks["experimental.idle"]({ sessionID: "ses_1" }),
    ])
    assert.equal(fake.calls().length, 1)
    assert.equal(one.state.synthetic.length + two.state.synthetic.length, 1)
  })

  test("sequentially, the second copy does nothing for a stop the first handled", async () => {
    const { fake, one, two } = await twoCopies({ check: "block" }, { messages: { ses_1: [assistant("msg_a")] } })
    await one.state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    await two.state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    assert.equal(fake.calls().length, 1)
    assert.equal(two.state.synthetic.length, 0)
    // The next stop belongs to whichever copy sees it first, and is again handled once.
    one.state.messages.ses_1.push(assistant("msg_b"))
    two.state.messages.ses_1.push(assistant("msg_b"))
    await two.state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    await one.state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    assert.equal(fake.calls().length, 2)
  })

  test("a new session starts once", async () => {
    const { fake, one, two } = await twoCopies({ start: "pass" }, {})
    one.state.subscriptions[0].stream.push(created("ses_new"))
    two.state.subscriptions[0].stream.push(created("ses_new"))
    assert.ok(await settle(() => fake.calls().length >= 1))
    await settle(() => false, 100)
    assert.equal(fake.calls().length, 1)
  })
})

describe("session start", () => {
  test("a new top-level session runs session-start and ignores its output", async () => {
    const { state, fake } = await start({ start: "block" })
    state.subscriptions[0].stream.push(created("ses_new"))
    assert.ok(await settle(() => fake.calls().length === 1))
    const [call] = fake.calls()
    assert.deepEqual(call.args, ["session-start", "--hook", "opencode"])
    assert.deepEqual(call.stdin, {
      session_id: "ses_new",
      cwd: state.directory,
      hook_event_name: "session.created",
      source: "startup",
    })
    await settle(() => false, 50)
    assert.equal(state.synthetic.length, 0)
    assert.deepEqual(warnings, [])
  })

  test("a child session does not start a session", async () => {
    const { state, fake } = await start({ start: "pass" })
    state.subscriptions[0].stream.push(created("ses_child", { parentID: "ses_1" }))
    state.subscriptions[0].stream.push({ type: "session.created", data: {} })
    state.subscriptions[0].stream.push({ type: "session.created" })
    await settle(() => false, 100)
    assert.equal(fake.calls().length, 0)
  })

  test("a failing session-start is only a warning", async () => {
    const { state, fake } = await start({ start: "error" })
    state.subscriptions[0].stream.push(created("ses_new"))
    assert.ok(await settle(() => warnings.length === 1))
    assert.equal(fake.calls().length, 1)
    assert.match(warnings[0], /greenproof could not run: exit 3/)
  })

  test("a failing event stream is a warning, and the idle hook still works", async () => {
    const fake = fakeGreenproof({ check: "block" })
    const plugin = await loadPlugin([fake.script])
    const { ctx, state } = fakeContext({ messages: { ses_1: [assistant("msg_a")] } })
    ctx.event.subscribe = () => ({
      [Symbol.asyncIterator]: () => ({ next: () => Promise.reject(new Error("stream closed")) }),
    })
    await plugin.default.setup(ctx)
    assert.ok(await settle(() => warnings.length === 1))
    assert.match(warnings[0], /no session start: stream closed/)
    await state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    assert.equal(state.synthetic.length, 1)
  })

  test("a context without ctx.event warns once and still gates the stop", async () => {
    const { state, fake } = await start({ check: "block" }, { withoutEvents: true, messages: { ses_1: [assistant("msg_a")] } })
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /no session events, so no session start/)
    await state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    assert.equal(fake.calls().length, 1)
  })
})

describe("a context that cannot gate", () => {
  test("without session.hook: one warning, no throw, session start still works", async () => {
    const { cleanup, state, fake } = await start({ start: "pass" }, { withoutHook: true })
    const hookWarnings = warnings.filter((line) => /no session\.hook/.test(line))
    assert.equal(hookWarnings.length, 1)
    assert.deepEqual(state.hooks, {})
    state.subscriptions[0].stream.push(created("ses_new"))
    assert.ok(await settle(() => fake.calls().length === 1))
    await cleanup()
  })

  test("a registration that throws: one warning, no throw", async () => {
    const { cleanup } = await start({}, { hookThrows: true })
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /could not register the idle hook: registration refused/)
    await cleanup()
  })

  test("a context with nothing at all does not throw", async () => {
    const plugin = await loadPlugin(["greenproof"])
    const cleanup = await plugin.default.setup({})
    await cleanup()
    assert.equal(warnings.length, 2)
  })
})

async function realpath(file) {
  const { realpath: resolve } = await import("node:fs/promises")
  return resolve(file)
}
