#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harbor TB2.1 全量评测实时监控面板（仅监听 127.0.0.1，纯标准库）。

用法: python3 monitor.py [port]   # 默认 8123
页面: http://127.0.0.1:8123/      API: /api/status
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import os
ROOT = os.environ.get("DSH_RUN_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOBS_ROOT = os.path.join(ROOT, "jobs")
TASKS_ROOT = os.environ.get("DSH_TASKS_DIR", os.path.join(ROOT, "tasks"))
HARBOR_LOG = os.environ.get("DSH_HARBOR_LOG", os.path.join(ROOT, "harbor.log"))
EXCLUDED = {"qemu-startup", "qemu-alpine-ssh"}
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
SERVER_START = time.time()
RE_JOB = re.compile(r"\d{4}-\d{2}-\d{2}__\d{2}-\d{2}-\d{2}")
STATUS_RANK = {"running": 0, "done": 1, "timeout": 2, "waiting": 3}


def latest_job_dir():
    override = os.environ.get("JOB_OVERRIDE")
    if override:
        p = os.path.join(JOBS_ROOT, override)
        if os.path.isdir(p):
            return p
    try:
        dirs = [d for d in os.listdir(JOBS_ROOT) if RE_JOB.fullmatch(d)]
    except OSError:
        return None
    return os.path.join(JOBS_ROOT, sorted(dirs)[-1]) if dirs else None


def job_start(jobdir):
    try:
        cfg = os.path.join(jobdir, "config.json")
        if os.path.exists(cfg):
            return os.path.getmtime(cfg)
        entries = [os.path.join(jobdir, e) for e in os.listdir(jobdir)]
        return min(os.path.getctime(e) for e in entries)
    except OSError:
        return SERVER_START


def task_list():
    try:
        return sorted(
            d for d in os.listdir(TASKS_ROOT)
            if os.path.isdir(os.path.join(TASKS_ROOT, d)) and d not in EXCLUDED
        )
    except OSError:
        return []


def docker_containers():
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return len([l for l in out.splitlines() if l.strip()])
    except Exception:
        return -1


def log_tail(n=8):
    try:
        with open(HARBOR_LOG, errors="replace") as f:
            lines = f.read().splitlines()
        kept = []
        for ln in lines:
            if "Wrote ATIF trajectory" in ln and kept and "Wrote ATIF trajectory" in kept[-1]:
                continue
            kept.append(ln)
        return kept[-n:]
    except OSError:
        return []


def scan_one(p, name):
    task = name.split("__")[0]
    result_path = os.path.join(p, "result.json")
    reward_path = os.path.join(p, "verifier", "reward.txt")
    exc_path = os.path.join(p, "exception.txt")
    traj_path = os.path.join(p, "agent", "trajectory.json")
    trial_log = os.path.join(p, "trial.log")

    reward, timed_out, done = None, False, False
    try:
        if os.path.exists(reward_path):
            with open(reward_path) as f:
                reward = float(f.read().strip())
            done = True
        elif os.path.exists(result_path):
            with open(result_path) as f:
                reward = json.load(f).get("reward")
            done = True
    except Exception:
        pass
    if os.path.exists(exc_path):
        try:
            with open(exc_path, errors="replace") as f:
                timed_out = "AgentTimeoutError" in f.read()
        except OSError:
            pass
    if done:
        status = "timeout" if (timed_out and not reward) else "done"
    else:
        status = "running"
    try:
        last = os.path.getmtime(trial_log)
    except OSError:
        last = os.path.getctime(p)
    return {
        "name": name,
        "task": task,
        "status": status,
        "reward": reward,
        "timed_out": timed_out,
        "last_activity": last,
        "has_traj": os.path.exists(traj_path),
    }


def scan_trials(jobdir):
    if not jobdir:
        return {}
    try:
        names = sorted(os.listdir(jobdir))
    except OSError:
        return {}
    out = {}
    for name in names:
        p = os.path.join(jobdir, name)
        if os.path.isdir(p):
            out[name.split("__")[0]] = scan_one(p, name)
    return out


def build_status():
    job = latest_job_dir()
    trials = scan_trials(job)
    rows = []
    for t in task_list():
        tr = trials.get(t)
        if tr:
            rows.append(tr)
        else:
            rows.append({"name": None, "task": t, "status": "waiting",
                         "reward": None, "timed_out": False,
                         "last_activity": 0, "has_traj": False})
    done = [r for r in rows if r["status"] in ("done", "timeout")]
    running = [r for r in rows if r["status"] == "running"]
    waiting = [r for r in rows if r["status"] == "waiting"]
    rewards = [r["reward"] for r in done if r["reward"] is not None]
    elapsed = time.time() - (job_start(job) if job else SERVER_START)
    eta_min = None
    if done:
        rate = len(done) / max(elapsed, 1)
        eta_min = round((len(rows) - len(done)) / rate / 60, 1)
    return {
        "job": os.path.basename(job) if job else None,
        "elapsed": elapsed,
        "total": len(rows),
        "done": len(done),
        "running": len(running),
        "waiting": len(waiting),
        "passed": sum(1 for v in rewards if v == 1),
        "reward_sum": round(sum(rewards), 2),
        "eta_min": eta_min,
        "docker": docker_containers(),
        "log_tail": log_tail(),
        "rows": sorted(rows, key=lambda r: (STATUS_RANK[r["status"]], r["task"])),
    }


INDEX = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TB2.1 全量评测监控 · DSH Minimal</title>
<style>
:root{color-scheme:dark}
body{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.muted{color:#8b93a3;font-size:12px}
.card{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:14px 18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}
.stat .v{font-size:24px;font-weight:700}
.stat .k{font-size:12px;color:#8b93a3;margin-top:2px}
.bar{height:10px;background:#232836;border-radius:6px;overflow:hidden;margin:14px 0 2px}
.bar>div{height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);transition:width .5s;border-radius:6px}
.pct{font-size:12px;color:#8b93a3}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #232836;white-space:nowrap}
th{color:#8b93a3;font-weight:600}
.scroll{max-height:56vh;overflow:auto;border:1px solid #232836;border-radius:10px;margin-top:12px}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px}
.b-running{background:#1e3a5f;color:#7cc4ff}
.b-done{background:#1a3a28;color:#6ee7a0}
.b-timeout{background:#3a2e1a;color:#ffcf7c}
.b-waiting{background:#232836;color:#8b93a3}
.r1{color:#6ee7a0;font-weight:700}
.r0{color:#ff7c7c;font-weight:700}
a{color:#7cc4ff;text-decoration:none}
a:hover{text-decoration:underline}
pre{white-space:pre-wrap;font-size:11px;color:#9aa4b5;max-height:150px;overflow:auto;margin:10px 0 0}
#last{float:right;font-size:12px;color:#8b93a3}
</style>
</head>
<body>
<h1>🐳 Harbor TB2.1 全量评测监控 · DSH Minimal <span class="muted" id="jobname"></span><span id="last"></span></h1>
<div class="card scorebar" style="display:flex;align-items:center;gap:24px;padding:18px 24px">
  <div style="font-size:44px;font-weight:800;color:#6ee7a0" id="scNum">-</div>
  <div>
    <div style="font-size:15px;font-weight:600" id="scSub">加载中...</div>
    <div class="muted" id="scDetail" style="margin-top:4px"></div>
  </div>
  <div style="flex:1"></div>
  <div class="muted" id="scEta"></div>
</div>
<div class="stats">
  <div class="card stat"><div class="v" id="sDone">-</div><div class="k">已完成 / 总数</div></div>
  <div class="card stat"><div class="v" id="sRun">-</div><div class="k">运行中</div></div>
  <div class="card stat"><div class="v" id="sWait">-</div><div class="k">排队中</div></div>
  <div class="card stat"><div class="v" id="sPass" style="color:#6ee7a0">-</div><div class="k">通过数 (reward=1)</div></div>
  <div class="card stat"><div class="v" id="sSum">-</div><div class="k">reward 总和</div></div>
  <div class="card stat"><div class="v" id="sElapsed">-</div><div class="k">已运行</div></div>
  <div class="card stat"><div class="v" id="sEta" style="color:#ffcf7c">-</div><div class="k">预计剩余</div></div>
  <div class="card stat"><div class="v" id="sDocker">-</div><div class="k">docker 容器</div></div>
</div>
<div class="bar"><div id="pbar" style="width:0%"></div></div>
<div class="pct" id="pct"></div>
<div class="scroll"><table>
<thead><tr><th>#</th><th>任务</th><th>状态</th><th>reward</th><th>轨迹</th><th>日志</th><th>最近活动</th></tr></thead>
<tbody id="rows"></tbody>
</table></div>
<pre id="logtail"></pre>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stLabel={running:'运行中',done:'完成',timeout:'超时',waiting:'排队'};
function fmt(sec){
  if(sec==null)return '—';
  sec=Math.max(0,Math.floor(sec));
  const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;
  return (h?h+'h ':'')+(m?m+'m ':'')+s+'s';
}
async function refresh(){
  try{
    const s=await (await fetch('/api/status')).json();
    document.title=`[${s.done}/${s.total}] TB2.1 全量评测监控 · DSH Minimal`;
    document.getElementById('jobname').textContent=s.job?('· '+s.job):'';
    document.getElementById('last').textContent='刷新于 '+new Date().toLocaleTimeString();
    const rateNum = s.done ? s.passed/s.done*100 : 0;
    const rateColor = rateNum >= 70 ? '#6ee7a0' : (rateNum >= 40 ? '#ffcf7c' : '#ff7c7c');
    document.getElementById('scNum').textContent=`${rateNum.toFixed(1)}%`;
    document.getElementById('scNum').style.color = rateColor;
    document.getElementById('scSub').textContent=`得分 ${s.passed}/${s.done} · 总进度 ${s.done}/${s.total} (${s.total?(s.done/s.total*100).toFixed(1):0}%)`;
    document.getElementById('scDetail').textContent=`运行中 ${s.running} · 排队 ${s.waiting} · reward 总和 ${s.reward_sum}`;
    document.getElementById('scEta').textContent=s.eta_min!=null?('预计剩余 '+fmt(s.eta_min*60)):'';
    document.getElementById('sDone').textContent=`${s.done}/${s.total}`;
    document.getElementById('sRun').textContent=s.running;
    document.getElementById('sWait').textContent=s.waiting;
    document.getElementById('sPass').textContent=s.passed;
    document.getElementById('sSum').textContent=s.reward_sum;
    document.getElementById('sElapsed').textContent=fmt(s.elapsed);
    document.getElementById('sEta').textContent=s.eta_min!=null?('~'+fmt(s.eta_min*60)):'—';
    document.getElementById('sDocker').textContent=s.docker>=0?s.docker:'?';
    const pct=s.total?Math.round(s.done/s.total*100):0;
    document.getElementById('pbar').style.width=pct+'%';
    document.getElementById('pct').textContent=`进度 ${pct}%`;
    const tb=document.getElementById('rows');tb.innerHTML='';
    s.rows.forEach((r,i)=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${i+1}</td><td>${esc(r.task)}</td>
        <td><span class="badge b-${r.status}">${stLabel[r.status]||r.status}</span></td>
        <td class="${r.reward==null?'':(r.reward===1?'r1':'r0')}">${r.reward==null?'—':r.reward}</td>
        <td>${r.has_traj?`<a href="/traj/${encodeURIComponent(r.name)}" target="_blank">查看</a>`:'—'}</td>
        <td>${r.name?`<a href="/log/${encodeURIComponent(r.name)}" target="_blank">查看</a>`:'—'}</td>
        <td class="muted">${r.last_activity?fmt(Date.now()/1000-r.last_activity)+'前':''}</td>`;
      tb.appendChild(tr);
    });
    document.getElementById('logtail').textContent='harbor log:\n'+s.log_tail.join('\n');
  }catch(e){document.getElementById('last').textContent='刷新失败: '+e;}
}
setInterval(refresh,4000);refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            payload = json.dumps(build_status(), ensure_ascii=False).encode()
            self._send(200, payload, "application/json; charset=utf-8")
            return
        job = latest_job_dir()
        if job and parsed.path.startswith("/traj/"):
            rel = parsed.path[len("/traj/"):]
            p = os.path.join(job, unquote(rel), "agent", "trajectory.json")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    self._send(200, f.read(), "application/json; charset=utf-8")
                return
        if job and parsed.path.startswith("/log/"):
            rel = parsed.path[len("/log/"):]
            p = os.path.join(job, unquote(rel), "trial.log")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/plain; charset=utf-8")
                return
        if parsed.path in ("/", "/index.html"):
            self._send(200, INDEX.encode(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[monitor] serving on http://127.0.0.1:{PORT}  (job: {os.path.basename(latest_job_dir() or '')})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
