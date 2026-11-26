import {test} from 'node:test'
import assert from 'node:assert/strict'
import {hostState,serviceState,diskView} from './hostops-view.mjs'
test('missing and stale heartbeats never render as online',()=>{
  assert.equal(hostState(undefined).color,'default')
  assert.equal(hostState('stale').color,'warning')
  assert.equal(hostState('online').color,'success')
})
test('invisible or failed services are not healthy',()=>{
  assert.equal(serviceState('not_visible').color,'default')
  assert.equal(serviceState('last_exit_failed').color,'error')
  assert.equal(serviceState('running').color,'success')
})
test('disk display rejects absent or malformed telemetry',()=>{
  assert.equal(diskView(null),null)
  assert.equal(diskView({total_bytes:0,used_bytes:0,free_bytes:0}),null)
  assert.equal(diskView({total_bytes:100,used_bytes:101,free_bytes:0}),null)
  assert.deepEqual(diskView({total_bytes:2**30,used_bytes:2**29,free_bytes:2**29}),{percent:50,free:'0.5',total:'1.0'})
})
