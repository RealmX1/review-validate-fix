#!/usr/bin/env python3
"""antipatterns-data.json → presentation.html（自包含单文件，内联 CSS/JS + JSON 数据岛）。

数据岛是 HTML 内的唯一真相源：JS 据此渲染幻灯；generate_markdown.py 读同一数据岛产 Markdown。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "antipatterns-data.json")
OUT = os.path.join(HERE, "presentation.html")

TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Agentic 工作流设计反模式</title>
<style>
:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--card:#161b22;--border:#30363d;--accent:#58a6ff;}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}
#deck{position:relative;height:100vh;width:100vw;overflow:hidden;}
.slide{position:absolute;inset:0;display:none;flex-direction:column;padding:4.5vh 6vw 8vh;overflow:auto;}
.slide.active{display:flex;animation:fade .25s ease;}
@keyframes fade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
h1{font-size:3.2vw;margin:.2em 0;line-height:1.15}
h2{font-size:2.1vw;margin:.1em 0 .6em}
.kicker{color:var(--accent);font-weight:600;letter-spacing:.08em;text-transform:uppercase;font-size:1vw}
.sub{color:var(--muted);font-size:1.2vw;line-height:1.5}
.cover-wrap{margin:auto;max-width:64vw}
ul{line-height:1.7;margin:.2em 0}
.cardhead{display:flex;align-items:baseline;gap:.9rem;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:.9rem;margin-bottom:1rem}
.cardhead .num{color:var(--muted);font-size:1.5vw}
.cardhead .cn{font-size:2.1vw;font-weight:700}
.cardhead .en{color:var(--muted);font-size:1.15vw}
.chip{font-size:.85vw;padding:.25em .7em;border-radius:999px;border:1px solid var(--border);color:var(--muted)}
.badge{font-size:.85vw;padding:.25em .7em;border-radius:999px;font-weight:600}
.ev-strong{background:rgba(63,185,80,.15);color:#3fb950;border:1px solid rgba(63,185,80,.4)}
.ev-mid{background:rgba(210,153,34,.15);color:#d29922;border:1px solid rgba(210,153,34,.4)}
.ev-weak{background:rgba(139,148,158,.15);color:#8b949e;border:1px solid rgba(139,148,158,.4)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1.3rem;flex:1;align-content:start}
.sec{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem}
.sec h3{margin:.1em 0 .5em;font-size:1.05vw;color:var(--accent);letter-spacing:.03em}
.sec p,.sec li{font-size:1.02vw;line-height:1.55;margin:.2em 0}
.sec.full{grid-column:1/3}
.sources a{color:var(--accent);text-decoration:none;font-size:.95vw}
.sources li{margin:.3em 0}
.bar{position:fixed;left:0;bottom:0;height:4px;background:var(--accent);transition:width .2s;z-index:60}
.counter{position:fixed;right:1.2vw;bottom:1.4vh;color:var(--muted);font-size:.9vw}
.hint{position:fixed;left:1.2vw;bottom:1.4vh;color:var(--muted);font-size:.85vw}
.gdivide{align-items:center;justify-content:center;text-align:center}
.gdivide .gno{font-size:1.3vw;color:var(--muted)}
.gdivide h1{font-size:4vw}
.maps{display:grid;grid-template-columns:1fr 1fr;gap:1.3rem;flex:1;align-content:start}
#overview{position:fixed;inset:0;background:rgba(13,17,23,.98);z-index:50;display:none;padding:4vh 5vw;overflow:auto}
#overview.show{display:block}
#overview h2{color:var(--accent)}
.ovitem{display:inline-block;margin:.25rem;padding:.4rem .8rem;border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:.92vw;background:var(--card)}
.ovitem:hover{border-color:var(--accent)}
code{background:#222a35;padding:.1em .4em;border-radius:5px;font-size:.92em}
</style>
</head>
<body>
<div id="deck"></div>
<div id="overview"></div>
<div class="bar" id="bar"></div>
<div class="counter" id="counter"></div>
<div class="hint">← → 翻页 · O 总览 · F 全屏</div>
<script type="application/json" id="antipatterns-data">__DATA_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('antipatterns-data').textContent);
const esc = s => (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const evClass = e => e==='跨多源公认'?'ev-strong':(e==='较公认'?'ev-mid':'ev-weak');
const groupTitle = id => { const g=DATA.groups.find(x=>x.id===id); return g?g.title_cn+' · '+g.title_en:id; };

function cardSlide(c){
  const sym = (c.symptoms||[]).map(s=>`<li>${esc(s)}</li>`).join('');
  const src = (c.sources||[]).map(s=>`<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a></li>`).join('');
  return `<section class="slide" data-title="#${esc(c.order)} ${esc(c.name_cn)}" data-group="${esc(c.group_id)}">
    <div class="cardhead">
      <span class="num">#${esc(c.order)}</span>
      <span class="cn">${esc(c.name_cn)}</span>
      <span class="en">${esc(c.name_en)}</span>
      <span class="chip">${esc(groupTitle(c.group_id))}</span>
      <span class="badge ${evClass(c.evidence_strength)}">${esc(c.evidence_strength)}</span>
    </div>
    <div class="grid">
      <div class="sec"><h3>定义 · What</h3><p>${esc(c.definition)}</p></div>
      <div class="sec"><h3>为何有害 · Why</h3><p>${esc(c.why_harmful)}</p></div>
      <div class="sec"><h3>症状：如何识别 · Symptoms</h3><ul>${sym}</ul></div>
      <div class="sec"><h3>缓解：正确做法 · Mitigation</h3><p>${esc(c.mitigation)}</p></div>
      <div class="sec full sources"><h3>来源 · Sources</h3><ul>${src}</ul></div>
    </div>
  </section>`;
}
function groupDivider(g,idx){
  const n = DATA.cards.filter(c=>c.group_id===g.id).length;
  return `<section class="slide gdivide" data-title="▎组${idx} ${esc(g.title_cn)}" data-group="${esc(g.id)}">
    <div><div class="gno">组 ${idx} · Group</div><h1>${esc(g.title_cn)}</h1><div class="sub">${esc(g.title_en)} — ${n} 个反模式</div></div>
  </section>`;
}

const cover = `<section class="slide" data-title="封面"><div class="cover-wrap">
  <div class="kicker">A Field Guide</div>
  <h1>AI Agentic 工作流设计反模式</h1>
  <div class="sub">业界公认实践 + 学术失败分类法的合并清单 — 供理解、评估，并据此审计 RVF</div>
  <p class="sub" style="margin-top:2rem">${DATA.cards.length} 个反模式 · ${DATA.groups.length} 组 · 证据强度三档标注</p>
</div></section>`;
const howto = `<section class="slide" data-title="如何阅读"><h2>如何阅读本演示</h2>
  <div class="grid">
   <div class="sec"><h3>每张卡片的结构</h3><ul><li><b>定义 / 为何有害 / 症状 / 缓解 / 来源</b></li><li>「症状」= 在真实代码库 / agent 系统里<b>可观察</b>的信号</li><li>「缓解」= 公认的对应正确 pattern</li></ul></div>
   <div class="sec"><h3>证据强度（卡片右上角徽标）</h3><ul><li><span class="badge ev-strong">跨多源公认</span> 多个独立权威来源一致</li><li><span class="badge ev-mid">较公认</span> 常见但来源相对集中</li><li><span class="badge ev-weak">新兴/单源观点</span> 单一来源或较新，需你独立判断</li></ul></div>
   <div class="sec full"><h3>评判主轴（贯穿全篇）</h3><p>Anthropic《Building Effective Agents》：<b>先求最简解，能用确定性 workflow 解决就别上 agent</b>。读每个反模式时不妨自问：这是不是「该用 workflow 却上了 agent」的某种变体？</p><p class="sub">读毕请在自动生成的 <code>anti-patterns.annotated.md</code> 里逐条标 同意 / 存疑 / 补充。</p></div>
  </div></section>`;
const map1 = `<section class="slide" data-title="地图① workflow vs agent"><h2>心智地图 ①：Workflow vs Agent</h2>
  <div class="maps">
   <div class="sec"><h3>Workflow（确定性编排）</h3><ul><li>预定义代码路径编排 LLM 与工具</li><li>可预测、可审计、可重放</li><li>适合：步骤已知、需一致性/合规</li></ul></div>
   <div class="sec"><h3>Agent（模型自主决策）</h3><ul><li>模型动态决定步骤与工具用法</li><li>灵活，但非确定、更贵、更难调试</li><li>适合：路径开放、需探索</li></ul></div>
   <div class="sec full"><h3>核心判据</h3><p>大量反模式的根因，要么是「在该用 workflow 的地方用了 agent」，要么反过来「该给 agent 自主权的地方写死了脆弱流程」。先把系统在这条轴上的位置定下来，再看每个反模式。</p></div>
  </div></section>`;
const map2 = `<section class="slide" data-title="地图② 失败分类法"><h2>心智地图 ②：两套失败分类法</h2>
  <div class="maps">
   <div class="sec"><h3>MAST（多智能体失败，Berkeley 2025）</h3><ul><li><b>系统设计</b>：规范/角色违背、步骤重复、不识别终止</li><li><b>智能体间错位</b>：信息隐瞒、忽视他者输入、推理-行动不一致</li><li><b>任务验证</b>：过早终止、验证缺失/错误</li></ul></div>
   <div class="sec"><h3>上下文失败四模式（Breunig / Chroma）</h3><ul><li><b>Poisoning</b> 幻觉进入上下文被反复引用</li><li><b>Distraction</b> 上下文过长压过训练知识</li><li><b>Confusion</b> 无关信息致工具/知识误用</li><li><b>Clash</b> 上下文内部自相矛盾</li></ul></div>
  </div></section>`;
const closing = `<section class="slide" data-title="评估指引"><div class="cover-wrap">
  <div class="kicker">Next</div><h1>该你了：评估这份清单</h1>
  <ul><li>打开自动生成的 <code>anti-patterns.annotated.md</code></li><li>逐条标 ☐同意 ☐存疑 ☐补充，写下你的既有认知 / 反驳</li><li>可增删反模式（改 <code>antipatterns-data.json</code> → 重跑 <code>build_html.py</code>）</li><li>确认最终清单后 → 进入 Phase B：用它审计 RVF 仓库</li></ul>
</div></section>`;

let slides = [cover, howto, map1, map2];
DATA.groups.forEach((g,i)=>{
  if(DATA.cards.some(c=>c.group_id===g.id)){
    slides.push(groupDivider(g,i+1));
    DATA.cards.filter(c=>c.group_id===g.id).sort((a,b)=>a.order-b.order).forEach(c=>slides.push(cardSlide(c)));
  }
});
slides.push(closing);

const deck = document.getElementById('deck');
deck.innerHTML = slides.join('');
const els = [...deck.querySelectorAll('.slide')];
let cur = 0;
function show(i){
  cur = Math.max(0, Math.min(els.length-1, i));
  els.forEach((s,j)=>s.classList.toggle('active', j===cur));
  document.getElementById('bar').style.width = ((cur+1)/els.length*100)+'%';
  document.getElementById('counter').textContent = (cur+1)+' / '+els.length;
  location.hash = 's'+cur;
}
function buildOverview(){
  const ov = document.getElementById('overview');
  let h = '<h2>总览 · Overview（点击跳转，O 关闭）</h2>';
  els.forEach((s,j)=>{ h += `<span class="ovitem" data-j="${j}">${esc(s.getAttribute('data-title'))}</span>`; });
  ov.innerHTML = h;
  ov.querySelectorAll('.ovitem').forEach(it=>it.onclick=()=>{ show(+it.dataset.j); toggleOv(false); });
}
function toggleOv(force){
  const ov = document.getElementById('overview');
  const on = force===undefined ? !ov.classList.contains('show') : force;
  ov.classList.toggle('show', on);
}
document.addEventListener('keydown', e=>{
  if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){ show(cur+1); e.preventDefault(); }
  else if(e.key==='ArrowLeft'||e.key==='PageUp'){ show(cur-1); }
  else if(e.key==='Home'){ show(0); }
  else if(e.key==='End'){ show(els.length-1); }
  else if(e.key.toLowerCase()==='o'){ toggleOv(); }
  else if(e.key.toLowerCase()==='f'){ if(!document.fullscreenElement) document.documentElement.requestFullscreen(); else document.exitFullscreen(); }
});
buildOverview();
const start = (location.hash.match(/^#s(\d+)$/)||[])[1];
show(start ? +start : 0);
</script>
</body>
</html>
'''


def main():
    if not os.path.exists(DATA):
        print(f"缺少 {DATA}；请先运行 assemble_data.py", file=sys.stderr)
        return 1
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA_JSON__", payload)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"生成 {OUT}（{len(data.get('cards', []))} 卡片，{len(html)} 字节，自包含无外部依赖）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
