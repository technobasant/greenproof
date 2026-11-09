// greenproof plugin for OpenCode. Written by greenproof init; re-running init replaces it.
//
// OpenCode 2.x only (@opencode/cli, plugin contract of @opencode/plugin). OpenCode has no
// command hooks, so this plugin runs `greenproof check --hook opencode` when a top-level
// session is about to go idle and maps greenproof's answer: a block continues the same run
// with greenproof's message, a release is shown as a warning, anything else lets the session
// stop. Every failure fails open; the CI gate is the backstop. Session start runs on the first
// prompt this process sees for a top-level session.
import { spawn } from "node:child_process"

const COMMAND = ["uvx", "greenproof@0.4.1"]

const TIMEOUT_MS = 3600 * 1000
const SESSION_START_TIMEOUT_MS = 120 * 1000
const KILL_GRACE_MS = 10 * 1000
const MAX_OUTPUT = 4 * 1024 * 1024
const REGISTRY = Symbol.for("greenproof.opencode")
const REGISTRY_LIMIT = 4096

function warn(message) {
  console.warn(message)
}

function describe(error) {
  return error instanceof Error ? error.message : String(error)
}

// Two copies of this plugin in one process (the npm package and init's file, or a user-level
// and a project-level copy) share one set of claimed keys, so an event is handled once.
function claim(key) {
  const claimed = (globalThis[REGISTRY] ??= new Set())
  if (claimed.has(key)) return false
  claimed.add(key)
  if (claimed.size > REGISTRY_LIMIT) claimed.delete(claimed.values().next().value)
  return true
}

// Runs `COMMAND ...args` without a shell, the JSON payload on stdin. Resolves with
// {stdout} or {error}; it never rejects.
function execute(args, payload, cwd, timeoutMs = TIMEOUT_MS) {
  return new Promise((resolve) => {
    let settled = false
    let child
    let timer
    const finish = (result) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(result)
    }
    try {
      child = spawn(COMMAND[0], [...COMMAND.slice(1), ...args], {
        cwd,
        shell: false,
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
      })
    } catch (error) {
      finish({ error: describe(error) })
      return
    }
    let stdout = ""
    let stderr = ""
    child.stdout.setEncoding("utf8")
    child.stderr.setEncoding("utf8")
    child.stdout.on("data", (chunk) => {
      if (stdout.length < MAX_OUTPUT) stdout += chunk
    })
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk).slice(-2000)
    })
    child.on("error", (error) => finish({ error: describe(error) }))
    child.on("close", (code, signal) => {
      if (code === 0) finish({ stdout })
      else finish({ error: `exit ${code ?? signal}${stderr.trim() ? `: ${stderr.trim()}` : ""}` })
    })
    // The child may exit without reading stdin; that is not an error here.
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
    timer = setTimeout(() => {
      child.kill("SIGTERM")
      setTimeout(() => child.kill("SIGKILL"), KILL_GRACE_MS).unref?.()
      finish({ error: `timed out after ${timeoutMs / 1000} seconds` })
    }, timeoutMs)
  })
}

// greenproof's answer, as {kind: "pass" | "block" | "release", text} or {error}.
function interpret(result) {
  if (result.error !== undefined) return { error: result.error }
  const text = result.stdout.trim()
  if (text === "") return { kind: "pass" }
  let output
  try {
    output = JSON.parse(text)
  } catch {
    return { error: "its output was not valid JSON" }
  }
  if (output === null || typeof output !== "object") return { error: "its output was not an object" }
  if (output.decision === "block" && typeof output.reason === "string" && output.reason !== "") {
    return { kind: "block", text: output.reason }
  }
  if (output.continue === false && typeof output.stopReason === "string") {
    return { kind: "release", text: output.stopReason }
  }
  return { error: "its output was not a known answer" }
}

// The id of the last assistant message of the session, or "" when it cannot be read.
async function lastAssistantMessageId(ctx, sessionID) {
  const messages = await ctx.session.context({ sessionID })
  if (!Array.isArray(messages)) return ""
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index]
    if (message && message.type === "assistant" && typeof message.id === "string" && message.id) {
      return message.id
    }
  }
  return ""
}

async function setup(ctx) {
  const cwd = ctx?.location?.directory || process.cwd()
  const running = new Set()
  const registrations = []

  async function isSubagent(sessionID) {
    try {
      const session = await ctx.session.get({ sessionID })
      return Boolean(session?.parentID)
    } catch (error) {
      warn(`greenproof could not read the session, so it does nothing: ${describe(error)}`)
      return true
    }
  }

  // Session start. OpenCode 2.x has no session-start hook, and a session.created event is
  // published before plugin subscriptions are live, so the first prompt stands in for it.
  // Never rejects: a rejected callback would fail the prompt.
  async function onPrompt(event) {
    try {
      const sessionID = event?.sessionID
      if (typeof sessionID !== "string" || sessionID === "") return
      // Claimed before any await, so repeated and concurrent prompts, and a second copy of
      // the plugin, start a session at most once.
      if (!claim(`session-start:${sessionID}:`)) return
      if (await isSubagent(sessionID)) return
      // An earlier assistant message means the session is being resumed, not started.
      let messageID
      try {
        messageID = await lastAssistantMessageId(ctx, sessionID)
      } catch (error) {
        warn(`greenproof could not read the session messages, so no session start: ${describe(error)}`)
        return
      }
      const payload = {
        session_id: sessionID,
        cwd,
        hook_event_name: "session.prompt",
        source: messageID === "" ? "startup" : "resume",
      }
      const result = await execute(["session-start", "--hook", "opencode"], payload, cwd, SESSION_START_TIMEOUT_MS)
      if (result.error !== undefined) warn(`greenproof could not run: ${result.error}`)
    } catch (error) {
      warn(`greenproof could not run: ${describe(error)}`)
    }
  }

  async function onIdle(event) {
    const sessionID = event?.sessionID
    if (typeof sessionID !== "string" || sessionID === "") return
    // A session whose first prompt this process never saw gets no session start after a check.
    claim(`session-start:${sessionID}:`)
    // One check per session at a time. Registered before the first await.
    if (running.has(sessionID)) return
    running.add(sessionID)
    try {
      if (await isSubagent(sessionID)) return
      let messageID = ""
      try {
        messageID = await lastAssistantMessageId(ctx, sessionID)
      } catch (error) {
        warn(`greenproof could not read the session messages: ${describe(error)}`)
      }
      if (messageID === "") {
        warn("greenproof did not run: the id of the last assistant message could not be read")
        return
      }
      if (!claim(`session.idle:${sessionID}:${messageID}`)) return
      const payload = {
        session_id: sessionID,
        cwd,
        hook_event_name: "session.idle",
        message_id: messageID,
      }
      const answer = interpret(await execute(["check", "--hook", "opencode"], payload, cwd))
      if (answer.error !== undefined) {
        warn(`greenproof could not run: ${answer.error}`)
      } else if (answer.kind === "block") {
        await ctx.session.synthetic({ sessionID, text: answer.text })
      } else if (answer.kind === "release") {
        warn(answer.text)
      }
    } catch (error) {
      warn(`greenproof could not run: ${describe(error)}`)
    } finally {
      running.delete(sessionID)
    }
  }

  if (typeof ctx?.session?.hook === "function") {
    try {
      registrations.push(await ctx.session.hook("experimental.idle", onIdle))
    } catch (error) {
      warn(`greenproof is inactive: could not register the idle hook: ${describe(error)}`)
    }
    try {
      registrations.push(await ctx.session.hook("prompt", onPrompt))
    } catch (error) {
      warn(`greenproof: could not register the prompt hook, so no session start: ${describe(error)}`)
    }
  } else {
    warn("greenproof is inactive: this OpenCode has no session.hook, so nothing gates a stop")
  }

  return async () => {
    for (const registration of registrations) {
      try {
        await registration?.dispose?.()
      } catch {
        // Unloading must not fail because of one registration.
      }
    }
  }
}

export default { id: "greenproof", setup }
