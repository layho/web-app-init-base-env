import {test} from 'node:test'
import assert from 'node:assert/strict'
import {boardRows, emptyBecauseNoRows, enabledActions, failureRows, historyRows} from './controlBoardView.ts'
import {workspaceTheme} from './workspace-theme.ts'

test('an unconfigured board has no invented rows', () => {
  const body = {state: 'not_configured', items: [], history: [], failures: [], tests: [], actions: []}
  assert.equal(emptyBecauseNoRows(body), true)
  assert.deepEqual(boardRows(body), [])
  assert.deepEqual(enabledActions(body), [])
})

test('rows keep the owner, time, and authority returned by the server', () => {
  const body = {
    state: 'available',
    items: [{
      task_id: 'tsk_w01',
      owner: {display: 'alice', evidence: 'recorded'},
      recorded_at: '2026-11-28T01:00:00Z',
      tests: [{task_id: 'tsk_w01', state: 'recorded', authoritative: false}],
      failure_reasons: [],
      advisory_failures: [{display: '日志失败', authoritative: false}],
      history: [{event_id: 'evt_task', occurred_at: '2026-11-28T01:00:00Z', state: 'PENDING', authoritative: false}],
    }],
    history: [{event_id: 'evt_task', occurred_at: '2026-11-28T01:00:00Z', state: 'PENDING', authoritative: false}],
    failures: [{display: '日志失败', authoritative: false, at: '2026-11-28T01:02:00Z'}],
    actions: [
      {id: 'unblock', enabled: true, calls_authority: true, connected_executor: true, task_id: 'tsk_w01'},
      {id: 'merge', enabled: false, calls_authority: true, code: 'NOT_AUTHORIZED'},
    ],
  }
  assert.deepEqual(boardRows(body), [{
    task_id: 'tsk_w01',
    owner: 'alice',
    owner_evidence: 'recorded',
    at: '2026-11-28T01:00:00Z',
    tests: 1,
    failures: 1,
  }])
  assert.equal(historyRows(body)[0].authoritative, false)
  assert.equal(failureRows(body)[0].text, '日志失败')
  assert.deepEqual(enabledActions(body).map(item => item.id), ['unblock'])
  assert.equal(emptyBecauseNoRows(body), false)
})

test('the shared shell uses the dark compact bar', () => {
  assert.equal(workspaceTheme.algorithm, 'dark')
  assert.equal(workspaceTheme.headerHeight, 48)
  assert.equal(workspaceTheme.token.colorBgLayout, '#0f1419')
  assert.notEqual(workspaceTheme.token.colorBgContainer, '#ffffff')
})
