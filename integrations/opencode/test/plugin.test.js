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

// A fake 2.x plugin context. `sessions` maps id to Session.Info, `messages` maps id to the
// SessionMessage.Info list of ctx.session.context.
function fakeContext(options = {}) {
  const directory = options.directory ?? freshDir("project")
  const state = {
    directory,
    hooks: {},
    synthetic: [],
    disposed: 0,
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
      if (options.hookThrows === true || options.hookThrows === name) {
        throw new Error("registration refused")
      }
      state.hooks[name] = callback
      return {
        dispose: async () => {
          state.disposed++
          if (options.disposeThrows === name) throw new Error("dispose failed")
        },
      }
    },
  }
  if (options.withoutHook) delete session.hook
  const ctx = {
    location: { directory },
    options: {},
    session,
  }
  return { ctx, state }
}

const assistant = (id) => ({ type: "assistant", id, content: [] })
const user = (id) => ({ type: "user", id })
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

  test("setup registers the idle and prompt hooks and returns a cleanup", async () => {
    const { state, cleanup } = await start()
    assert.deepEqual(Object.keys(state.hooks).sort(), ["experimental.idle", "prompt"])
    assert.equal(typeof cleanup, "function")
  })

  test("cleanup disposes both registrations, even when one dispose fails", async () => {
    const { state, cleanup } = await start({}, { disposeThrows: "experimental.idle" })
    await cleanup()
    assert.equal(state.disposed, 2)
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

})

const prompt = (state, sessionID = "ses_1") => state.hooks.prompt({ sessionID, messageID: "m", prompt: "hi" })

describe("session start (the prompt hook)", () => {
  test("a new session runs session-start before the callback resolves, and ignores its output", async () => {
    const { state, fake } = await start({ start: "slow", delayMs: 300 })
    const began = Date.now()
    await prompt(state)
    assert.ok(Date.now() - began >= 250, "the callback resolved before the command finished")
    const [call, ...rest] = fake.calls()
    assert.deepEqual(rest, [])
    assert.deepEqual(call.args, ["session-start", "--hook", "opencode"])
    assert.deepEqual(call.stdin, {
      session_id: "ses_1",
      cwd: state.directory,
      hook_event_name: "session.prompt",
      source: "startup",
    })
    assert.equal(state.synthetic.length, 0)
    assert.deepEqual(warnings, [])
  })

  test("a session with an earlier assistant message is a resume", async () => {
    const { state, fake } = await start({ start: "pass" }, { messages: { ses_1: [user("u"), assistant("msg_a")] } })
    await prompt(state)
    assert.equal(fake.calls().length, 1)
    assert.equal(fake.calls()[0].stdin.source, "resume")
  })

  test("a second prompt in the same session does not start it again", async () => {
    const { state, fake } = await start({ start: "pass" })
    await prompt(state)
    await prompt(state)
    await Promise.all([prompt(state), prompt(state)])
    assert.equal(fake.calls().length, 1)
    await prompt(state, "ses_2")
    assert.deepEqual(fake.calls().map((call) => call.stdin.session_id), ["ses_1", "ses_2"])
  })

  test("concurrent prompts run it once", async () => {
    const { state, fake } = await start({ start: "slow", delayMs: 200 })
    await Promise.all([prompt(state), prompt(state), prompt(state)])
    assert.equal(fake.calls().length, 1)
  })

  test("two copies in one process start a session once", async () => {
    const fake = fakeGreenproof({ start: "pass" })
    const [first, second] = await Promise.all([loadPlugin([fake.script]), loadPlugin([fake.script])])
    const one = fakeContext()
    const two = fakeContext({ directory: one.state.directory })
    await first.default.setup(one.ctx)
    await second.default.setup(two.ctx)
    await Promise.all([prompt(one.state), prompt(two.state)])
    await prompt(two.state)
    await prompt(one.state)
    assert.equal(fake.calls().length, 1)
  })

  test("a prompt after an idle on the same session does not start it", async () => {
    const { state, fake } = await start({ check: "pass" }, { messages: { ses_1: [assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    await prompt(state)
    assert.deepEqual(fake.calls().map((call) => call.args[0]), ["check"])
  })

  test("a continuation that passes through the prompt hook after a check does not start it", async () => {
    const { state, fake } = await start({ check: "block" }, { messages: { ses_1: [assistant("msg_a")] } })
    await state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    assert.equal(state.synthetic.length, 1)
    await prompt(state)
    assert.deepEqual(fake.calls().map((call) => call.args[0]), ["check"])
  })

  test("a child session is skipped", async () => {
    const { state, fake } = await start({ start: "pass" }, { sessions: { ses_1: { id: "ses_1", parentID: "ses_0" } } })
    await prompt(state)
    assert.equal(fake.calls().length, 0)
    assert.deepEqual(warnings, [])
  })

  test("a session that cannot be read is skipped with a warning", async () => {
    const { state, fake } = await start({ start: "pass" }, { getThrows: true })
    await prompt(state)
    assert.equal(fake.calls().length, 0)
    assert.match(warnings[0], /could not read the session, so it does nothing/)
  })

  test("messages that cannot be read: no session start, a warning", async () => {
    const { state, fake } = await start({ start: "pass" }, { contextThrows: true })
    await prompt(state)
    assert.equal(fake.calls().length, 0)
    assert.match(warnings[0], /could not read the session messages, so no session start: context failed/)
  })

  test("a failing command only warns, and the callback resolves", async () => {
    const { state } = await start({ start: "error" })
    await prompt(state)
    assert.match(warnings[0], /greenproof could not run: exit 3: boom happened/)
  })

  test("a command that cannot be spawned only warns, and the callback resolves", async () => {
    const { state } = await start({}, {}, [path.join(ROOT, "no-such-greenproof")])
    await prompt(state)
    assert.match(warnings[0], /greenproof could not run: .*ENOENT/)
  })

  test("a session start past 120 seconds is killed, and the callback resolves", async () => {
    const { state, fake } = await start({ start: "hang" })
    mock.timers.enable({ apis: ["setTimeout"] })
    const done = prompt(state)
    assert.ok(await settle(() => fake.calls().length === 1), "the fake was never started")
    mock.timers.tick(120 * 1000 - 1)
    await settle(() => false, 20)
    assert.equal(warnings.length, 0)
    mock.timers.tick(1)
    await done
    assert.deepEqual(warnings, ["greenproof could not run: timed out after 120 seconds"])
    mock.timers.tick(10_000)
  })

  test("an event without a session id does nothing, and the event is never modified", async () => {
    const { state, fake } = await start({ start: "pass" })
    await state.hooks.prompt({ prompt: "hi" })
    await state.hooks.prompt(undefined)
    await state.hooks.prompt({ sessionID: "", prompt: "hi" })
    const event = { sessionID: "ses_1", messageID: "m", prompt: "hi", metadata: { a: 1 }, delivery: "immediate" }
    const before = structuredClone(event)
    await state.hooks.prompt(event)
    assert.deepEqual(event, before)
    assert.deepEqual(fake.calls().map((call) => call.stdin.session_id), ["ses_1"])
  })
})

describe("a context that cannot gate", () => {
  test("without session.hook: one warning, no throw", async () => {
    const { cleanup, state } = await start({}, { withoutHook: true })
    assert.equal(warnings.filter((line) => /no session\.hook/.test(line)).length, 1)
    assert.equal(warnings.length, 1)
    assert.deepEqual(state.hooks, {})
    await cleanup()
  })

  test("both registrations throwing: a warning for each, no throw", async () => {
    const { cleanup } = await start({}, { hookThrows: true })
    assert.equal(warnings.length, 2)
    assert.match(warnings[0], /could not register the idle hook: registration refused/)
    assert.match(warnings[1], /could not register the prompt hook, so no session start: registration refused/)
    await cleanup()
  })

  test("the prompt registration throwing leaves the idle gate working", async () => {
    const { state, fake } = await start({ check: "block" }, { hookThrows: "prompt", messages: { ses_1: [assistant("msg_a")] } })
    assert.deepEqual(Object.keys(state.hooks), ["experimental.idle"])
    assert.equal(warnings.length, 1)
    assert.match(warnings[0], /could not register the prompt hook, so no session start/)
    await state.hooks["experimental.idle"]({ sessionID: "ses_1" })
    assert.equal(fake.calls().length, 1)
    assert.equal(state.synthetic.length, 1)
  })

  test("the idle registration throwing leaves session start working", async () => {
    const { state, fake } = await start({ start: "pass" }, { hookThrows: "experimental.idle" })
    assert.deepEqual(Object.keys(state.hooks), ["prompt"])
    await prompt(state)
    assert.equal(fake.calls().length, 1)
  })

  test("a context with nothing at all does not throw", async () => {
    const plugin = await loadPlugin(["greenproof"])
    const cleanup = await plugin.default.setup({})
    await cleanup()
    assert.equal(warnings.length, 1)
  })
})

async function realpath(file) {
  const { realpath: resolve } = await import("node:fs/promises")
  return resolve(file)
}
