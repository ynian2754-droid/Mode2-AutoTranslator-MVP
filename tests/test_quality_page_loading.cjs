"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/quality-page.js"), "utf8");

function page(summary, status) {
  const calls = [], elements = new Map();
  const document = {
    addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {value:"", options:[], dataset:{}, querySelectorAll:()=>[]});
      return elements.get(id);
    },
  };
  const context = vm.createContext({document, window:{addEventListener() {}},
    location:{search:""}, URLSearchParams, AbortController, qualityEscape:String,
    setTimeout:()=>1, clearTimeout() {}, calls, status, summary,
    renders:0, captures:0, failStatus:false});
  vm.runInContext(source + `
    qRequest=async url=>{
      calls.push(url);
      if(url.includes('/prepare/status')) {
        if(failStatus) throw new Error('offline');
        return structuredStatus();
      }
      if(url.endsWith('/affected-units')) return {affected:[]};
      if(url.endsWith('/quality-support')) return {prepare:summary, cards:[], reference_mode:'automatic'};
      return {project:{id:'runtime'}, current_project:{id:'catalog'}, units:[]};
    };
    structuredStatus=()=>JSON.parse(JSON.stringify(status));
    qCheckProject=()=>{};
    qCaptureDraft=()=>{captures++;};
    qRenderCards=()=>{renders++;};
    qControls=()=>{};
    qFinishLeave=()=>{};
    qInvalidate=()=>{};
    qRenderUnits=()=>{};
    qRenderTerminologyAudit=()=>{};
    qRenderHistory=()=>{};
    qRenderPrepare=()=>{};
    qWriteReset=()=>{};
    globalThis.api={load:qLoad, poll:qPollPrepareStatus, merge:qMergePrepareStatus, qp};
  `, context);
  return context;
}

function terminal(summary, revision=1) {
  return {project_id:"runtime", prepare_id:"task", active:false,
    status:summary.status, persisted_status:summary.status,
    progress_revision:revision, prepare:summary};
}
const finish = () => new Promise(resolve=>setImmediate(resolve));

for (const revision of [1, null]) {
  test(`initial terminal results render once (revision ${revision})`, async()=>{
    const summary={prepare_id:"task", status:"partial", adopted:3};
    const ctx=page(summary, terminal(summary, revision));
    await ctx.api.load(true);
    await finish();
    assert.equal(ctx.calls.length,5);
    assert.equal(ctx.calls.filter(url=>url.endsWith("/quality-support")).length,1);
    assert.equal(ctx.renders,1);
    assert.equal(ctx.api.qp.prepareProgress.status,"partial");
  });
}

test("completion during initial loading refreshes the changed results", async()=>{
  const ctx=page({prepare_id:"task", status:"running", adopted:0},
    terminal({prepare_id:"task", status:"complete", adopted:4}));
  await ctx.api.load(true);
  await finish();
  assert.equal(ctx.calls.length,7);
  assert.equal(ctx.renders,2);
  assert.equal(ctx.captures,2);
});

test("changed terminal summary is refreshed even when the task id is unchanged", async()=>{
  const ctx=page({prepare_id:"task", status:"partial", adopted:3},
    terminal({prepare_id:"task", status:"partial", adopted:4}));
  await ctx.api.load(true);
  await finish();
  assert.equal(ctx.renders,2);
});

test("running to terminal transition refreshes once and captures the open draft", async()=>{
  const summary={prepare_id:"task", status:"running"};
  const ctx=page(summary,{project_id:"runtime",prepare_id:"task",active:true,
    status:"running",progress_revision:1,prepare:summary});
  await ctx.api.load(true);
  assert.equal(ctx.renders,1);
  ctx.status=terminal({prepare_id:"task",status:"complete"},2);
  await ctx.api.poll({manual:true});
  await finish();
  assert.equal(ctx.renders,2);
  assert.equal(ctx.captures,2);
  await ctx.api.poll({manual:true});
  await finish();
  assert.equal(ctx.renders,2);
});

test("offline initial status read refreshes results on terminal recovery", async()=>{
  const summary={prepare_id:"task",status:"partial"};
  const ctx=page(summary,terminal(summary));
  ctx.failStatus=true;
  await ctx.api.load(true);
  assert.equal(ctx.renders,1);
  ctx.failStatus=false;
  await ctx.api.poll({manual:true});
  await finish();
  assert.equal(ctx.renders,2);
  assert.equal(ctx.api.qp.connectionIssue,"");
});

test("foreign project or task status never refreshes or overwrites loaded results", async()=>{
  const summary={prepare_id:"task",status:"partial"};
  const ctx=page(summary,terminal(summary));
  await ctx.api.load(true);
  assert.equal(ctx.api.merge({...terminal(summary,2),project_id:"other"}),false);
  assert.equal(ctx.api.merge({...terminal(summary,2),prepare_id:"other"}),false);
  await finish();
  assert.equal(ctx.renders,1);
});

test("unknown execute outcome still refreshes after terminal confirmation", async()=>{
  const summary={prepare_id:"task",status:"complete"};
  const ctx=page(summary,terminal(summary));
  ctx.api.qp.runtimeId="runtime";
  ctx.api.qp.observedPrepareId="task";
  ctx.api.qp.prepareOutcomeUnknown=true;
  await ctx.api.poll({manual:true});
  await finish();
  assert.equal(ctx.renders,1);
  assert.equal(ctx.api.qp.prepareOutcomeUnknown,false);
});
