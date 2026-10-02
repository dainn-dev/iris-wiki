import assert from "node:assert/strict"
import { test } from "node:test"
import { createSyncProgress, progressFromSnapshot, updateSyncProgress, completedFileCount } from "../src/lib/gdrive-sync-state.ts"

const files = [
  { id: "a", name: "Report.pdf" },
  { id: "b", name: "Report.pdf" },
]

function listed() {
  return updateSyncProgress(createSyncProgress("demo"), { event: "files", data: { files } })
}

test("files stay pending until backend reports actual progress, keyed by ID", () => {
  const initial = listed()
  const processing = updateSyncProgress(initial, {
    event: "file", data: { id: "a", name: "Report.pdf", status: "processing" },
  })
  assert.equal(processing.files[0].status, "processing")
  assert.equal(processing.files[1].status, "pending")
  assert.equal(initial.files[0].status, "pending")
  assert.equal(completedFileCount(processing), 0)
  const done = updateSyncProgress(processing, {
    event: "file", data: { id: "a", name: "Report.pdf", status: "added" },
  })
  assert.equal(completedFileCount(done), 1)
  assert.equal(done.phase, "syncing")
})

test("skipped, failed and removed files all count as processed", () => {
  for (const status of ["skipped", "failed", "removed"]) {
    const state = updateSyncProgress(listed(), {
      event: "file", data: { id: "b", name: "Report.pdf", status },
    })
    assert.equal(completedFileCount(state), 1)
    assert.equal(state.files[1].status, status)
  }
})

test("a fatal error preserves completed files and stops unfinished spinners", () => {
  let state = updateSyncProgress(listed(), {
    event: "file", data: { id: "a", name: "Report.pdf", status: "added" },
  })
  state = updateSyncProgress(state, { event: "error", data: { message: "Reconnect Google Drive." } })
  assert.equal(state.phase, "error")
  assert.equal(state.error, "Reconnect Google Drive.")
  assert.equal(state.files[0].status, "added")
  assert.equal(state.files[1].status, "interrupted")
  assert.equal(completedFileCount(state), 1)
  assert.equal(state.result, null)
})

test("only final completes a run and retains the summary including partial failures", () => {
  const state = listed()
  assert.equal(updateSyncProgress(state, { event: "done", data: {} }).phase, "syncing")
  const result = { kb: "demo", added: 1, skipped: 0, failed: 1, removed: 0 }
  const completed = updateSyncProgress(state, { event: "final", data: result })
  assert.equal(completed.phase, "complete")
  assert.deepEqual(completed.result, result)
})

test("empty folders finish with zero processed files", () => {
  const state = updateSyncProgress(createSyncProgress("demo"), {
    event: "files", data: { files: [] },
  })
  assert.equal(completedFileCount(state), 0)
  assert.equal(state.phase, "syncing")
})

test("a cancelled final keeps processed files and interrupts the rest", () => {
  let state = updateSyncProgress(listed(), {
    event: "file", data: { id: "a", name: "Report.pdf", status: "added" },
  })
  const result = { kb: "demo", added: 1, skipped: 0, failed: 0, removed: 0, cancelled: 1 }
  state = updateSyncProgress(state, { event: "final", data: result })
  assert.equal(state.phase, "cancelled")
  assert.equal(state.files[0].status, "added")
  assert.equal(state.files[1].status, "interrupted")
  assert.equal(completedFileCount(state), 1)
})

test("a server snapshot restores the popup after a page reload", () => {
  const running = progressFromSnapshot({
    kb: "demo",
    phase: "running",
    files: [
      { id: "a", name: "Report.pdf", status: "added" },
      { id: "b", name: "Report.pdf", status: "downloading" },
    ],
    result: null,
    error: null,
  })
  assert.equal(running.phase, "syncing")
  assert.equal(running.files[1].status, "downloading")
  assert.equal(completedFileCount(running), 1)

  const done = progressFromSnapshot({
    kb: "demo",
    phase: "done",
    files: [{ id: "a", name: "Report.pdf", status: "added" }],
    result: { kb: "demo", added: 1, skipped: 0, failed: 0, removed: 0 },
    error: null,
  })
  assert.equal(done.phase, "complete")
  assert.equal(done.result.added, 1)

  const cancelled = progressFromSnapshot({
    kb: "demo",
    phase: "cancelled",
    files: [
      { id: "a", name: "Report.pdf", status: "added" },
      { id: "b", name: "Report.pdf", status: "pending" },
    ],
    result: null,
    error: null,
  })
  assert.equal(cancelled.phase, "cancelled")
  assert.equal(cancelled.files[1].status, "interrupted")
})
